"""Price-action S/R retest strategy — decoded from Hindi livestream.

Core rules:
  - Daily trend filter: only buy dips in uptrend, sell rallies in downtrend.
  - Trade at key S/R levels only (avoid mid-range).
  - Wait for a strong reversal candle with small wick (= buyer/seller aggression).
  - Wider SL, big target (1:7 R:R) — lets the S/R retest breathe.
  - Trail stop to breakeven after +1R.

The strategy builds its own 1-minute OHLC bars from live mark updates so it
does not depend on option-chain data. This makes it a pure perp strategy.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import pandas as pd

from strategies.crypto_base import CryptoStrategy, CryptoSignalDecision

logger = logging.getLogger(__name__)

# Production dials — see delta_exchange/backtest_price_action_sweep.py.
# 3-month backtest (Apr–Jun 2026) selected config:
#   BTC: SL 0.6% / 1:7   (124 trades, WR 57.3%, PF 1.79, +17.28%, MaxDD 2.52%, MaxCL 5)
#   ETH: SL 0.7% / 1:7   ( 83 trades, WR 56.6%, PF 2.01, +18.10%, MaxDD 2.33%, MaxCL 3)
# Wider stops vs the earlier 0.4%/0.5% baseline raise win-rate by letting the
# S/R retest breathe, while the 1:7 target keeps the R:R attractive.
LOOKBACK_CANDLES = 240            # 4h S/R range
TREND_CANDLES    = 1440           # 24h trend
RANGE_PCT_MAX    = 0.015          # max 1.5% range width
RANGE_PCT_MIN    = 0.0            # min range width (0 = disabled)
ZONE_PCT         = 0.004          # within 0.4% of level
BODY_MULT        = 1.3
WICK_RATIO_MAX   = 0.45
BODY_POS_THRESHOLD = 0.70         # long close_pos >= x, short <= 1-x
WICK_TOUCH_TOL   = 0.0007         # wick must touch/pierce S/R level within 7 bps
RETEST_MODE      = "wick_touch"   # "zone" | "wick_touch" | "strong_rejection" | "two_touch"
SL_PCT           = 0.005          # 0.5% base SL
RR_RATIO         = 7.0            # 1:7 target
ASSET_DIALS      = {              # per-asset overrides
    "BTC": {"sl_pct": 0.006, "rr_ratio": 7.0},
    "ETH": {"sl_pct": 0.007, "rr_ratio": 7.0},
}
BREAKEVEN_R      = 1.0            # trail SL to entry after +1R
MAX_HOLD_MINUTES = 240            # 4h max hold
COOLDOWN_MINUTES = 60             # 1h between signals
BLOCK_AFTER_LOSS_MINUTES = 180    # block same-side re-entry after a losing trade

# WR-boost filters (all default off / neutral)
MIN_VOLUME_MULT  = 1.0            # current candle volume >= x * 4h avg (live: skip if no volume)
RSI_PERIOD       = 14
RSI_LONG_MAX     = 100            # max RSI for long entries (100 = disabled)
RSI_SHORT_MIN    = 0              # min RSI for short entries (0 = disabled)
TREND_SLOPE_CANDLES = 0           # trend MA slope lookback (0 = disabled)
TREND_SLOPE_MIN_PCT = 0.0         # min |trend MA slope %| over lookback
TRADING_HOURS    = "all"          # UTC ranges, e.g. "0-4,13-21"
HTF_ALIGN        = False          # require 15m trend alignment
HTF_1H_SLOPE_MIN_PCT = 0.0        # min |1h EMA20 slope %| over 20 candles (0 = disabled)
VOL_FILTER_MAX   = 0.0            # max 24h realized vol to trade (0 = disabled)
REQUIRE_ENGULFING = False         # require engulfing candle pattern
PIN_BAR_WICK_RATIO = 0.0          # min wick/range for pin-bar (0 = disabled)


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float


class PriceActionSRSignal(CryptoStrategy):
    """Pure price-action S/R retest strategy."""

    name: str = "price_action_sr"
    underlying: str = ""
    # Per-asset overrides for the 24h realized-vol filter.  0.0 = disabled.
    vol_filter_max: float = VOL_FILTER_MAX

    @property
    def symbol(self) -> str:
        return f"{self.underlying}USD"

    def __init__(self, broker=None):
        super().__init__(broker)
        self._candles: deque[Candle] = deque(maxlen=TREND_CANDLES + 10)
        self._current_bar: Optional[dict] = None
        self._last_signal_minute: int = 0
        self._last_decision: Optional[CryptoSignalDecision] = None
        self._last_state: dict = {}
        self._last_loss_minute: dict = {"buy": 0, "sell": 0}
        self._htf_candles: deque[Candle] = deque(maxlen=200)
        self._current_htf_bar: Optional[dict] = None
        self._1h_candles: deque[Candle] = deque(maxlen=60)
        self._current_1h_bar: Optional[dict] = None

    def _parse_trading_hours(self, s: str):
        """Parse '0-4,13-21' into list of (start, end) UTC hour tuples."""
        if not s or s.lower() == "all":
            return []
        ranges = []
        for part in s.split(","):
            part = part.strip()
            if "-" not in part: continue
            a, b = part.split("-", 1)
            ranges.append((int(a), int(b)))
        return ranges

    def _time_allowed(self, ts: float) -> bool:
        """Check if UTC hour is inside configured trading windows."""
        ranges = self._parse_trading_hours(TRADING_HOURS)
        if not ranges:
            return True
        hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        for start, end in ranges:
            if start < end:
                if start <= hour < end: return True
            else:
                if hour >= start or hour < end: return True
        return False

    def _rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """Latest RSI value."""
        if len(closes) < period + 1:
            return 50.0
        s = pd.Series(closes)
        delta = s.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0

    def _build_htf_bar(self, mark: float) -> None:
        """Build 15m higher-timeframe bars from incoming marks."""
        now = time.time()
        minute = int(now // 60)
        htf_minute = (minute // 15) * 15
        if self._current_htf_bar is None or self._current_htf_bar["minute"] != htf_minute:
            if self._current_htf_bar is not None:
                b = self._current_htf_bar
                self._htf_candles.append(Candle(
                    ts=b["minute"] * 60, open=b["open"], high=b["high"],
                    low=b["low"], close=b["close"],
                ))
                while len(self._htf_candles) > 200:
                    self._htf_candles.popleft()
            self._current_htf_bar = {"minute": htf_minute, "open": mark, "high": mark, "low": mark, "close": mark}
        else:
            self._current_htf_bar["high"] = max(self._current_htf_bar["high"], mark)
            self._current_htf_bar["low"] = min(self._current_htf_bar["low"], mark)
            self._current_htf_bar["close"] = mark

    def _htf_trend(self) -> tuple[bool, bool]:
        """Return (htf_bullish, htf_bearish) based on 15m close vs 20-candle MA."""
        candles = list(self._htf_candles)
        if len(candles) < 20:
            return True, True
        closes = np.array([c.close for c in candles])
        ma = closes.mean()
        last = closes[-1]
        return last > ma, last < ma

    def _build_1h_bar(self, mark: float) -> None:
        """Build 1h bars from incoming marks for trend-strength filter."""
        now = time.time()
        minute = int(now // 60)
        h1_minute = (minute // 60) * 60
        if self._current_1h_bar is None or self._current_1h_bar["minute"] != h1_minute:
            if self._current_1h_bar is not None:
                b = self._current_1h_bar
                self._1h_candles.append(Candle(
                    ts=b["minute"] * 60, open=b["open"], high=b["high"],
                    low=b["low"], close=b["close"],
                ))
                while len(self._1h_candles) > 60:
                    self._1h_candles.popleft()
            self._current_1h_bar = {"minute": h1_minute, "open": mark, "high": mark, "low": mark, "close": mark}
        else:
            self._current_1h_bar["high"] = max(self._current_1h_bar["high"], mark)
            self._current_1h_bar["low"] = min(self._current_1h_bar["low"], mark)
            self._current_1h_bar["close"] = mark

    def _1h_trend_strength(self) -> tuple[bool, bool]:
        """Return (strong_bullish, strong_bearish) based on 1h EMA20 slope."""
        candles = list(self._1h_candles)
        if len(candles) < 20:
            return True, True
        closes = pd.Series([c.close for c in candles])
        ema20 = closes.ewm(span=20, min_periods=10).mean()
        slope = ema20.iloc[-1] - ema20.iloc[-20]
        threshold = HTF_1H_SLOPE_MIN_PCT / 100.0 * ema20.iloc[-1]
        bullish = ema20.iloc[-1] > ema20.iloc[-2] and slope > threshold
        bearish = ema20.iloc[-1] < ema20.iloc[-2] and slope < -threshold
        return bullish, bearish

    def _realized_vol_24h(self) -> float:
        """Annualized 24h realized volatility from 1m closes."""
        candles = list(self._candles)
        if len(candles) < 24 * 60:
            return 0.0
        closes = pd.Series([c.close for c in candles[-24 * 60:]])
        returns = closes.pct_change().dropna()
        if returns.empty:
            return 0.0
        return float(returns.std() * np.sqrt(365 * 24 * 60))

    def notify_trade_closed(self, side: str, pnl_pct: float) -> None:
        """Runner calls this when a strategy trade closes. Used for block-after-loss."""
        if pnl_pct <= 0:
            self._last_loss_minute[side] = int(time.time() // 60)

    def _blocked_by_loss(self, side: str) -> bool:
        if BLOCK_AFTER_LOSS_MINUTES <= 0:
            return False
        last = self._last_loss_minute.get(side, 0)
        return (int(time.time() // 60) - last) < BLOCK_AFTER_LOSS_MINUTES

    def _record_mark(self, mark: float) -> None:
        """Bucket incoming mark prices into 1m candles (and 15m HTF bars)."""
        now = time.time()
        minute = int(now // 60)
        if self._current_bar is None or self._current_bar["minute"] != minute:
            if self._current_bar is not None:
                b = self._current_bar
                self._candles.append(Candle(
                    ts=b["minute"] * 60,
                    open=b["open"],
                    high=b["high"],
                    low=b["low"],
                    close=b["close"],
                ))
                # trim to max needed length
                while len(self._candles) > TREND_CANDLES + 10:
                    self._candles.popleft()
            self._current_bar = {"minute": minute, "open": mark, "high": mark, "low": mark, "close": mark}
        else:
            self._current_bar["high"] = max(self._current_bar["high"], mark)
            self._current_bar["low"] = min(self._current_bar["low"], mark)
            self._current_bar["close"] = mark
        if HTF_ALIGN:
            self._build_htf_bar(mark)
        if HTF_1H_SLOPE_MIN_PCT > 0:
            self._build_1h_bar(mark)

    def _signal(self) -> Optional[CryptoSignalDecision]:
        candles = list(self._candles)
        if len(candles) < TREND_CANDLES:
            return None

        opens = np.array([c.open for c in candles])
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        closes = np.array([c.close for c in candles])
        idx = len(candles) - 1
        close = closes[idx]

        r_high = highs[-LOOKBACK_CANDLES:].max()
        r_low = lows[-LOOKBACK_CANDLES:].min()
        width_pct = (r_high - r_low) / close

        # daily trend via long-term moving average
        trend_closes = closes[-TREND_CANDLES:]
        trend_ma = trend_closes.mean()
        allow_long = close > trend_ma
        allow_short = close < trend_ma

        # trend slope filter
        if TREND_SLOPE_CANDLES > 0 and TREND_SLOPE_MIN_PCT > 0:
            if len(trend_closes) > TREND_SLOPE_CANDLES:
                old_ma = trend_closes[:-TREND_SLOPE_CANDLES].mean() if TREND_SLOPE_CANDLES > 0 else trend_ma
                slope = (trend_ma - old_ma) / trend_ma
                allow_long = allow_long and (slope >= TREND_SLOPE_MIN_PCT)
                allow_short = allow_short and (slope <= -TREND_SLOPE_MIN_PCT)

        body = abs(closes[idx] - opens[idx])
        rng = highs[idx] - lows[idx]
        green = closes[idx] > opens[idx]
        red = closes[idx] < opens[idx]
        close_pos = (closes[idx] - lows[idx]) / rng if rng > 0 else 0.5
        upper_wick = highs[idx] - max(closes[idx], opens[idx])
        lower_wick = min(closes[idx], opens[idx]) - lows[idx]
        wick_pct = (upper_wick + lower_wick) / rng if rng > 0 else 0.0

        avg_body = np.mean([abs(c - o) for c, o in zip(closes[-LOOKBACK_CANDLES:], opens[-LOOKBACK_CANDLES:])])

        # volume confirmation (live: we don't have per-tick volume, so this is a no-op unless wired)
        vol_ok = True
        if MIN_VOLUME_MULT > 1.0:
            try:
                stats = self.broker.get_futures_stats().get(self.symbol, {})
                vol_24h = float(stats.get("volume_24h_usd") or 0)
                # crude proxy: require non-zero 24h volume; real per-candle volume needs WS upgrade
                vol_ok = vol_24h > 0
            except Exception:
                vol_ok = True

        # RSI momentum filter
        if RSI_PERIOD > 0:
            rsi_val = self._rsi(closes, RSI_PERIOD)
            rsi_long_ok = rsi_val <= RSI_LONG_MAX
            rsi_short_ok = rsi_val >= RSI_SHORT_MIN
        else:
            rsi_long_ok = rsi_short_ok = True

        # higher-timeframe alignment
        if HTF_ALIGN:
            htf_long, htf_short = self._htf_trend()
        else:
            htf_long = htf_short = True

        # 1h trend strength filter
        if HTF_1H_SLOPE_MIN_PCT > 0:
            strong_long, strong_short = self._1h_trend_strength()
            allow_long = allow_long and strong_long
            allow_short = allow_short and strong_short

        # 24h volatility filter
        vol_filter_ok = True
        if self.vol_filter_max > 0:
            vol_filter_ok = self._realized_vol_24h() <= self.vol_filter_max

        # candlestick patterns
        pattern_long_ok = pattern_short_ok = True
        if REQUIRE_ENGULFING or PIN_BAR_WICK_RATIO > 0:
            pattern_long_ok = pattern_short_ok = False
            if idx >= 1:
                prev_o, prev_c = opens[idx-1], closes[idx-1]
                prev_green, prev_red = prev_c > prev_o, prev_c < prev_o
                if REQUIRE_ENGULFING:
                    # current green body engulfs previous red body
                    if green and prev_red and o <= prev_c and c >= prev_o:
                        pattern_long_ok = True
                    # current red body engulfs previous green body
                    if red and prev_green and o >= prev_c and c <= prev_o:
                        pattern_short_ok = True
                if PIN_BAR_WICK_RATIO > 0:
                    body_ratio = body / rng if rng > 0 else 1
                    if green and (lower_wick / rng >= PIN_BAR_WICK_RATIO) and body_ratio <= 0.35:
                        pattern_long_ok = True
                    if red and (upper_wick / rng >= PIN_BAR_WICK_RATIO) and body_ratio <= 0.35:
                        pattern_short_ok = True

        strong_green = (green and body >= BODY_MULT * avg_body and wick_pct <= WICK_RATIO_MAX and
                        close_pos >= BODY_POS_THRESHOLD and vol_ok and rsi_long_ok and
                        htf_long and vol_filter_ok and pattern_long_ok)
        strong_red = (red and body >= BODY_MULT * avg_body and wick_pct <= WICK_RATIO_MAX and
                      close_pos <= (1 - BODY_POS_THRESHOLD) and vol_ok and rsi_short_ok and
                      htf_short and vol_filter_ok and pattern_short_ok)

        # retest-quality gate
        near_high = (r_high - close) / close <= ZONE_PCT
        near_low = (close - r_low) / close <= ZONE_PCT
        wick_high = (r_high - highs[idx]) / close <= WICK_TOUCH_TOL
        wick_low = (lows[idx] - r_low) / close <= WICK_TOUCH_TOL

        if RETEST_MODE == "zone":
            retest_long_ok = near_low
            retest_short_ok = near_high
        elif RETEST_MODE == "wick_touch":
            retest_long_ok = wick_low
            retest_short_ok = wick_high
        elif RETEST_MODE == "strong_rejection":
            retest_long_ok = wick_low and (close_pos >= BODY_POS_THRESHOLD)
            retest_short_ok = wick_high and (close_pos <= (1 - BODY_POS_THRESHOLD))
        elif RETEST_MODE == "two_touch":
            # at least 2 of the last 3 candles touched the level
            recent_lows = lows[-3:]
            recent_highs = highs[-3:]
            low_touches = int(sum((recent_lows - r_low) / close <= ZONE_PCT))
            high_touches = int(sum((r_high - recent_highs) / close <= ZONE_PCT))
            retest_long_ok = (low_touches >= 2) and (close_pos >= BODY_POS_THRESHOLD)
            retest_short_ok = (high_touches >= 2) and (close_pos <= (1 - BODY_POS_THRESHOLD))
        else:
            raise ValueError(f"Unknown RETEST_MODE: {RETEST_MODE}")

        # cooldown: max one signal per hour
        current_minute = int(time.time() // 60)
        in_cooldown = current_minute - self._last_signal_minute < COOLDOWN_MINUTES

        dials = ASSET_DIALS.get(self.underlying, {"sl_pct": SL_PCT, "rr_ratio": RR_RATIO})
        sl_pct = dials["sl_pct"]
        rr_ratio = dials["rr_ratio"]

        # time-of-day and block-after-loss filters (must be computed BEFORE the
        # dashboard snapshot below uses them).
        current_ts = time.time()
        time_ok = self._time_allowed(current_ts)
        block_long = self._blocked_by_loss("buy")
        block_short = self._blocked_by_loss("sell")

        # snapshot state for the dashboard even if we don't fire
        self._last_state = {
            "close": float(close),
            "r_high": float(r_high),
            "r_low": float(r_low),
            "width_pct": float(width_pct * 100),
            "trend": "bullish" if allow_long else ("bearish" if allow_short else "neutral"),
            "near_support": bool(near_low),
            "near_resistance": bool(near_high),
            "wick_touch_support": bool(wick_low),
            "wick_touch_resistance": bool(wick_high),
            "strong_green": bool(strong_green),
            "strong_red": bool(strong_red),
            "in_cooldown": bool(in_cooldown),
            "time_ok": bool(time_ok),
            "block_long": bool(block_long),
            "block_short": bool(block_short),
            "htf_1h_slope_ok": bool(allow_long or allow_short) if HTF_1H_SLOPE_MIN_PCT > 0 else True,
            "vol_24h": float(self._realized_vol_24h()),
            "vol_filter_ok": bool(vol_filter_ok),
            "sl_pct": float(sl_pct),
            "tp_pct": float(sl_pct * rr_ratio),
        }

        # market-condition / data-quality gates (applied AFTER dashboard snapshot)
        width_ok = (width_pct <= RANGE_PCT_MAX and
                    (RANGE_PCT_MIN <= 0 or width_pct >= RANGE_PCT_MIN) and
                    rng > 0)

        side = None
        if (width_ok and allow_long and retest_long_ok and strong_green and
                not in_cooldown and time_ok and not block_long):
            side = "buy"
            sl = lows[idx] * 0.9998
            sl_dist = max(sl_pct, (close - sl) / close)
            tp = close * (1 + sl_dist * rr_ratio)
        elif (width_ok and allow_short and retest_short_ok and strong_red and
                not in_cooldown and time_ok and not block_short):
            side = "sell"
            sl = highs[idx] * 1.0002
            sl_dist = max(sl_pct, (sl - close) / close)
            tp = close * (1 - sl_dist * rr_ratio)
        else:
            self._last_decision = None
            return None

        self._last_signal_minute = current_minute
        self._record_pred_trace((1 if side == "buy" else -1) * width_pct * 100)
        self._record_sig_history((1 if side == "buy" else -1) * width_pct * 100)

        decision = CryptoSignalDecision(
            name=self.name,
            symbol=self.symbol,
            side=side,
            pred_pct=width_pct * 100,
            n_strikes=1,
            size_mult=1.0,
            stop_loss_pct=float(sl_dist),
            partial_tp_pct=float(sl_dist * rr_ratio),
            trail_peak_pct=float(sl_dist * BREAKEVEN_R),
            trail_giveback=float(sl_dist * 0.25),
            metadata={
                "r_high": float(r_high),
                "r_low": float(r_low),
                "close": float(close),
                "sl": float(sl),
                "tp": float(tp),
            },
        )
        self._last_decision = decision
        return decision

    def update_bars(self, mark: float) -> None:
        """Call frequently (e.g. every 2s) to build accurate 1m candles."""
        if mark is not None and mark > 0:
            self._record_mark(mark)

    def latest_state(self) -> dict:
        """Read-only snapshot for the dashboard. Safe to call from API."""
        return {
            "underlying": self.underlying,
            "symbol": self.symbol,
            "ready": len(self._candles) >= TREND_CANDLES,
            "retest_mode": RETEST_MODE,
            **self._last_state,
            "last_decision": {
                "side": self._last_decision.side,
                "pred_pct": self._last_decision.pred_pct,
                "stop_loss_pct": self._last_decision.stop_loss_pct,
                "partial_tp_pct": self._last_decision.partial_tp_pct,
                "metadata": self._last_decision.metadata,
            } if self._last_decision else None,
        }

    def _compute_signal(self) -> Optional[CryptoSignalDecision]:
        mark = self.broker.get_perp_mark(self.symbol)
        if mark is None or mark <= 0:
            return None
        self._record_mark(mark)
        return self._signal()


class BTCPriceActionSRSignal(PriceActionSRSignal):
    name = "btc_price_action_sr"
    underlying = "BTC"
    # Vol filter degraded BTC performance in Apr–Jul 2026 backtests; keep disabled.
    vol_filter_max = 0.0


class ETHPriceActionSRSignal(PriceActionSRSignal):
    name = "eth_price_action_sr"
    underlying = "ETH"
    # 34% was the sweet spot in fixed-capital backtests (80%+ WR, ~5% MaxDD).
    vol_filter_max = 0.34
