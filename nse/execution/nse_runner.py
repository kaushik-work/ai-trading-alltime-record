"""NSE live execution runner.

Schedules entry and position-management ticks during market hours only.
Entry evaluates the synthetic-forward signal every 5 minutes.
Position management checks SL / TP / trail / max-hold every 30 seconds.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger

from core.ipc import is_market_holiday
from core.utils import now_ist
from data.angel_fetcher import AngelFetcher
from nse.broker.angel_broker import AngelBroker
from nse.config import (
    MARKET_CLOSE,
    MARKET_OPEN,
    MAX_HOLD_HOURS,
    STEP_SIZES,
    STOP_LOSS_PCT,
    SYMBOLS,
    TARGET_PCT,
    TICK_ENTRY_MINUTES,
    TICK_POSITION_SECONDS,
    TOTAL_CAPITAL_INR,
    TRAIL_GIVEBACK_PCT,
    TRAIL_PEAK_PCT,
)
from nse.data.option_chain import OptionChainCache
from nse.models import Position, SyntheticForwardSignal
from nse.risk import (
    add_day_pnl,
    check_kill_switch,
    is_killed,
)
from nse.strategies.synthetic_forward import SyntheticForwardStrategy

logger = logging.getLogger(__name__)

# In-memory runtime state
_OPEN_POSITIONS: dict[str, Position] = {}
_SIG_HISTORY: dict[str, dict[datetime, list[tuple[datetime, float]]]] = {}
_DAY_JOURNAL: list[dict] = []
_MARGIN_USED_INR: float = 0.0
_INITIALIZED = False


def _margin_available() -> float:
    return TOTAL_CAPITAL_INR - _MARGIN_USED_INR


def _is_market_open(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = now_ist()
    if now.weekday() >= 5:
        return False
    is_hol, _ = is_market_holiday(now.date().isoformat())
    if is_hol:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _entry_tick():
    if not _is_market_open():
        return
    if check_kill_switch():
        logger.info("NSE entry tick skipped: kill switch active")
        return
    if _margin_available() <= 0:
        logger.debug("NSE entry tick skipped: no free margin")
        return

    logger.debug("NSE entry tick")
    try:
        fetcher = AngelFetcher.get()
        broker = AngelBroker(fetcher)

        # Collect gated signals and live margins from all symbols.
        tradable: list[tuple[str, SyntheticForwardSignal, list, float, OptionChainCache]] = []
        for symbol in SYMBOLS:
            cache = OptionChainCache(symbol, fetcher)
            spot = cache.get_underlying_ltp()
            if spot is None:
                continue
            expiry = cache.nearest_expiry(min_days=0)
            if expiry is None:
                continue
            step = STEP_SIZES[symbol]
            atm = int(round(spot / step)) * step
            snapshot = cache.get_snapshot(expiry, atm, strikes_around=8)
            if snapshot.empty:
                continue

            strategy = SyntheticForwardStrategy(symbol)
            t = datetime.now(timezone.utc)
            sigs = strategy.compute(snapshot, t)

            # Mirror raw preds for observability.
            for s in sigs:
                try:
                    from core import mongo
                    mongo.mirror_nse_signal({
                        "ts": t.isoformat(),
                        "symbol": s.symbol,
                        "expiry": s.expiry.isoformat(),
                        "pred": s.pred,
                        "synth_forward": s.synth_forward,
                        "spot": s.spot,
                        "n_strikes": s.n_strikes,
                        "side": s.side,
                    })
                except Exception as e:
                    logger.debug("nse_signals mirror failed: %s", e)

            # Record raw preds for persistence.
            hist = _SIG_HISTORY.setdefault(symbol, {})
            for s in sigs:
                hist.setdefault(s.expiry, []).append((t, s.pred))

            # Trim history.
            for e in list(hist.keys()):
                hist[e] = [(ti, pi) for ti, pi in hist[e]
                           if (t - ti).total_seconds() <= 6 * 3600]

            candidates = sorted(sigs, key=lambda s: abs(s.pred), reverse=True)
            chosen: Optional[SyntheticForwardSignal] = None
            for c in candidates:
                if strategy.gate(c, hist):
                    chosen = c
                    break
            if chosen is None:
                continue

            # Resolve 1 lot and ask Angel One for exact margin.
            legs = cache.resolve_combo(atm, expiry, chosen.side, 1)
            if not legs:
                continue
            margin = broker.get_combo_margin_required(legs)
            if margin is None or margin <= 0:
                logger.warning("NSE entry tick: margin API failed for %s", symbol)
                continue
            for leg in legs:
                leg.entry_px = _estimate_leg_entry(leg, snapshot)
            tradable.append((symbol, chosen, legs, margin, cache))

        if not tradable:
            return

        # Trade the strongest signals first until capital is exhausted.
        tradable.sort(key=lambda x: abs(x[1].pred), reverse=True)
        for symbol, chosen, legs, margin, cache in tradable:
            available = _margin_available()
            if margin > available:
                logger.info("NSE entry: skipping %s, needs %.0f, available %.0f",
                            symbol, margin, available)
                continue

            sl_points = chosen.spot * STOP_LOSS_PCT
            target_points = chosen.spot * TARGET_PCT
            position = broker.place_combo(chosen, legs, use_limit=True,
                                          sl_points=sl_points, target_points=target_points)
            if position is None:
                continue

            global _MARGIN_USED_INR
            _MARGIN_USED_INR += margin
            position.margin_used = margin
            _OPEN_POSITIONS[position.position_id] = position
            _journal_event("ENTRY", position, chosen.spot)
            logger.info("NSE ENTRY %s | %s %s | lots=1 | margin=%.0f | pred=%.3f%% | positions=%d",
                        position.position_id, symbol, chosen.side, margin,
                        chosen.pred * 100, len(_OPEN_POSITIONS))

            if _margin_available() <= 0:
                break
    except Exception as e:
        logger.exception("NSE entry tick failed: %s", e)


def _estimate_leg_entry(leg, snapshot: "pd.DataFrame") -> float:
    """Estimate entry price from snapshot LTP."""
    row = snapshot[(snapshot["strike"] == leg.strike) &
                   (snapshot["option_type"] == leg.option_type)]
    if row.empty:
        return 0.0
    return float(row.iloc[0]["ltp"])


def _position_tick():
    if not _is_market_open():
        return
    if not _OPEN_POSITIONS:
        return

    try:
        fetcher = AngelFetcher.get()
        broker = AngelBroker(fetcher)
        now = datetime.now(timezone.utc)
        for pid in list(_OPEN_POSITIONS.keys()):
            pos = _OPEN_POSITIONS[pid]
            spot = fetcher.get_index_ltp(pos.symbol)
            if spot is None or spot <= 0:
                continue

            side = 1 if pos.signal_side == "long" else -1
            unreal = side * (spot - pos.spot_at_entry) / pos.spot_at_entry
            pos.peak_pnl_pct = max(pos.peak_pnl_pct, unreal)

            reason = None
            held_h = (now - pos.entry_time).total_seconds() / 3600
            if now >= pos.max_hold_until:
                reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                reason = "max_hold"
            elif unreal < -pos.stop_loss_pct:
                reason = "stop"
            elif unreal >= pos.target_pct:
                reason = "target"
            elif pos.peak_pnl_pct >= TRAIL_PEAK_PCT and (pos.peak_pnl_pct - unreal) > TRAIL_GIVEBACK_PCT:
                reason = "trail"

            if reason:
                pos.exit_reason = reason
                pos.exit_time = now
                pos.status = "CLOSED"
                # Actual P&L for one lot: spot_move × lot_size.
                lot_size = LOT_SIZES.get(pos.symbol, 1)
                pnl = side * (spot - pos.spot_at_entry) * lot_size
                pos.pnl = pnl
                add_day_pnl(pnl)
                broker.close_combo(pos)
                global _MARGIN_USED_INR
                _MARGIN_USED_INR = max(0.0, _MARGIN_USED_INR - getattr(pos, "margin_used", 0.0))
                _journal_event(reason, pos, spot)
                logger.info("NSE EXIT %s | %s | pnl=%.0f | spot=%.2f",
                            pid, reason, pnl, spot)
                del _OPEN_POSITIONS[pid]
    except Exception as e:
        logger.exception("NSE position tick failed: %s", e)


def _journal_event(reason: str, position: Position, spot: float):
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "position_id": position.position_id,
        "symbol": position.symbol,
        "event": reason,
        "signal_side": position.signal_side,
        "pred_pct": position.pred_pct,
        "spot": spot,
        "pnl": position.pnl if reason != "ENTRY" else 0.0,
        "mode": "live",
    }
    _DAY_JOURNAL.append(event)
    try:
        from core import mongo
        mongo.mirror_nse_event(event)
    except Exception as e:
        logger.warning("nse_trades mirror failed: %s", e)


def init_nse_runner(scheduler: BaseScheduler) -> bool:
    """Register NSE jobs with the APScheduler. Returns True if registered."""
    import platform
    global _INITIALIZED
    if _INITIALIZED:
        return True
    from nse.risk import ENABLE_NSE_RUNNER
    if not ENABLE_NSE_RUNNER:
        logger.info("NSE runner disabled (ENABLE_NSE_RUNNER=false)")
        return False
    if platform.system() != "Linux" and os.environ.get("ALLOW_LOCAL_SCHEDULER") != "1":
        logger.warning("NSE runner refuses to start on %s; set ALLOW_LOCAL_SCHEDULER=1 to override",
                       platform.system())
        return False

    # Entry every 5 minutes.
    ist = ZoneInfo("Asia/Kolkata")
    scheduler.add_job(
        _entry_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{TICK_ENTRY_MINUTES}", timezone=ist),
        id="nse_entry_tick",
        replace_existing=True,
    )
    # Position management every 30 seconds during market hours.
    scheduler.add_job(
        _position_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-15", second=f"*/{TICK_POSITION_SECONDS}", timezone=ist),
        id="nse_position_tick",
        replace_existing=True,
    )
    _INITIALIZED = True
    logger.info("NSE runner initialized (live only)")
    return True


def get_nse_runner_state() -> dict:
    """Return serializable runtime state for the API.

    Includes broker RMS when available so the dashboard reflects real limits,
    not just internal accounting.
    """
    broker_rms = {}
    try:
        fetcher = AngelFetcher.get()
        # Only hit RMS if already logged in; avoid blocking the status endpoint
        # with a full TOTP re-login on every 5-second poll.
        if fetcher.is_token_live():
            rms = fetcher.get_rms() or {}
            broker_rms = {
                "available_cash": rms.get("availablecash"),
                "available_limit": rms.get("availablelimitmargin"),
                "net": rms.get("net"),
                "utiliseddebits": rms.get("utiliseddebits"),
            }
    except Exception as e:
        logger.debug("get_nse_runner_state: broker RMS fetch failed: %s", e)

    return {
        "enabled": _INITIALIZED,
        "mode": "live",
        "killed": is_killed(),
        "day_pnl": round(_day_pnl(), 2),
        "unrealized_pnl": round(_unrealized_pnl(), 2),
        "total_capital": TOTAL_CAPITAL_INR,
        "margin_used": round(_MARGIN_USED_INR, 2),
        "margin_available": round(_margin_available(), 2),
        "broker_rms": broker_rms,
        "open_positions": [
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "side": p.signal_side,
                "entry_time": p.entry_time.isoformat(),
                "pred_pct": p.pred_pct,
                "spot_at_entry": p.spot_at_entry,
                "max_hold_until": p.max_hold_until.isoformat(),
                "legs": [
                    {"side": l.side, "type": l.option_type, "strike": l.strike,
                     "lots": l.lots, "filled_px": l.filled_px}
                    for l in p.legs
                ],
            }
            for p in _OPEN_POSITIONS.values()
        ],
        "journal_count": len(_DAY_JOURNAL),
    }


# Fix import reference for day_pnl
def _day_pnl() -> float:
    from nse.risk import get_day_pnl
    return get_day_pnl()


def _unrealized_pnl() -> float:
    """Return mark-to-market P&L of all open positions using live spot."""
    if not _OPEN_POSITIONS:
        return 0.0
    try:
        fetcher = AngelFetcher.get()
        total = 0.0
        for pos in _OPEN_POSITIONS.values():
            spot = fetcher.get_index_ltp(pos.symbol)
            if spot is None or spot <= 0:
                continue
            side = 1 if pos.signal_side == "long" else -1
            lot_size = LOT_SIZES.get(pos.symbol, 1)
            total += side * (spot - pos.spot_at_entry) * lot_size
        return total
    except Exception as e:
        logger.debug("_unrealized_pnl failed: %s", e)
        return 0.0
