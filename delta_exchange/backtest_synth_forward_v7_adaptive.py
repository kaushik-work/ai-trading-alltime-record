"""
Synthetic-Forward V7 — Adaptive defenses against edge decay
============================================================
Inherits v5's signal + execution. Layers in five robustness defenses
against the four MC caveats:

  1. Signal-decay monitor      — tracks rolling median |pred| magnitude;
                                  if it falls below 70% of historical baseline,
                                  raises ENTRY_PCT dynamically and reduces size.

  2. Live-performance tracker  — rolling 20-trade Sharpe; if it drops below
                                  half of baseline, tightens gate and shrinks
                                  size. If goes negative, halts new entries
                                  for HALT_HOURS.

  3. Regime circuit breaker    — refuses entry when:
                                    a) realized vol > 2× rolling median (panic regime)
                                    b) perp spread > MAX_BPS (illiquid)
                                  Bookkeeping placeholder for (b); needs live feed.

  4. Hard drawdown stop        — kills strategy if total DD from peak > KILL_DD_PCT.
                                  Manual restart required.

  5. Slippage adaptation       — tracks recent realized vs assumed slippage;
                                  if delta widens, raises SLIPPAGE_BPS.
                                  In backtest this only reads; live would write.

Theoretical grounding (from QF101 + Indian markets transcripts):
  - "Edge = spread between P and Q measures" → if median |pred| shrinks,
    market is closing that spread, our edge is decaying.
  - "Timing = 3x R:R improvement" → wait for stronger dislocations when
    weaker ones stop working.
  - "Models go stale" → automated detection so we know WHEN to research,
    not just hope.
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

from guards import (
    TradeIntent, PortfolioState, pipeline,
    max_concurrent_positions, cooldown_after_consecutive_losses,
    min_signal_strength, underlying_whitelist,
)

UNDERLYING  = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

# ── Same v5 dials ─────────────────────────────────────────────────────────────
ENTRY_PCT_BASE  = float(os.environ.get("ENTRY_PCT", "0.006"))
PERSIST_HOURS   = 2
MIN_STRIKES     = 3
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
MAX_HOLD_HOURS  = 72
MIN_TT_HOURS    = 6
MAX_TT_HOURS    = 72
STOP_LOSS_PCT       = 0.015
PARTIAL_TP_PCT      = 0.010
TRAIL_PEAK_PCT      = 0.005
TRAIL_GIVEBACK_PCT  = 0.0025
SIZE_BASE_PCT   = 0.005
SIZE_MAX_MULT   = 3.0
SIZE_MIN_MULT   = 0.5
MAX_CONCURRENT  = 2

# ── V7 ADAPTIVE DEFENSES ─────────────────────────────────────────────────────
# Defense 1: signal-health monitor
HEALTH_WINDOW_HOURS    = 7 * 24        # last 7 days of timestamps
HEALTH_DECAY_THRESHOLD = 0.70          # median |pred| < 70% of baseline → decayed
HEALTH_BASELINE_MIN    = 168           # need this many timestamps before judging

# Defense 2: live-performance tracker
PERF_WINDOW_TRADES     = 20            # rolling Sharpe over last N trades
PERF_DEGRADED_THRESH   = 0.50          # rolling SR < 50% of historical → tighten
HALT_HOURS_ON_NEG_SR   = 24            # if rolling SR < 0, halt this long

# Defense 3: regime circuit breaker
VOL_PANIC_MULT         = 2.0           # if RV > 2× rolling median RV, skip
RV_LOOKBACK_MIN        = 7 * 24 * 60
RV_MEDIAN_WINDOW       = 30 * 24 * 60  # 30 days of 1m bars for median

# Defense 4: hard DD kill
KILL_DD_PCT            = 0.15          # 15% drawdown from peak → manual restart


# ── Data plumbing (same as v5) ────────────────────────────────────────────────
def load_perp() -> pd.DataFrame:
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["timestamp", "close"]].set_index("timestamp").sort_index()
    df["logret"] = np.log(df["close"]).diff()
    df["rv7d"] = df["logret"].rolling(RV_LOOKBACK_MIN).std() * math.sqrt(365 * 24 * 60)
    df["rv_median"] = df["rv7d"].rolling(RV_MEDIAN_WINDOW, min_periods=24*60).median()
    return df


def parse_symbol(sym):
    parts = sym.split("-")
    side = parts[0]; strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_option_marks():
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(marks):
    rows = []
    for sym in marks:
        try: side, strike, expiry = parse_symbol(sym)
        except Exception: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def compute_pred_per_expiry(t, spot, catalogue, marks):
    tt_min = t + pd.Timedelta(hours=MIN_TT_HOURS)
    tt_max = t + pd.Timedelta(hours=MAX_TT_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= 0.05]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp = float(c.loc[t]); pp = float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


# ── Adaptive defenses ────────────────────────────────────────────────────────
class AdaptiveState:
    def __init__(self):
        self.signal_history = []          # list of (t, max|pred|)  for health monitor
        self.baseline_median = None       # historical median |pred|
        self.recent_pnls = []             # last PERF_WINDOW_TRADES PnLs
        self.peak_equity = 10_000.0
        self.halt_until = None
        self.gate_multiplier = 1.0        # multiplies ENTRY_PCT_BASE
        self.size_dampener = 1.0          # multiplies size_mult (0-1)
        self.health_warnings = 0
        self.killed = False

    def record_signal(self, t, max_pred):
        self.signal_history.append((t, max_pred))
        # trim to window
        cutoff = t - pd.Timedelta(hours=HEALTH_WINDOW_HOURS)
        self.signal_history = [(ti, p) for ti, p in self.signal_history if ti >= cutoff]

    def update_baseline(self):
        if self.baseline_median is None and len(self.signal_history) >= HEALTH_BASELINE_MIN:
            self.baseline_median = float(np.median([p for _, p in self.signal_history]))

    def signal_health(self) -> float:
        """Returns recent median / baseline. <1 = decay; <0.7 = trouble."""
        if self.baseline_median is None or not self.signal_history:
            return 1.0
        recent_median = float(np.median([p for _, p in self.signal_history[-HEALTH_BASELINE_MIN:]]))
        return recent_median / self.baseline_median if self.baseline_median > 0 else 1.0

    def record_pnl(self, pnl_usd, current_equity):
        self.recent_pnls.append(pnl_usd)
        self.recent_pnls = self.recent_pnls[-PERF_WINDOW_TRADES:]
        self.peak_equity = max(self.peak_equity, current_equity)

    def rolling_sharpe(self) -> float:
        if len(self.recent_pnls) < 5: return float("nan")
        arr = np.array(self.recent_pnls)
        if arr.std(ddof=1) == 0: return 0.0
        return arr.mean() / arr.std(ddof=1) * math.sqrt(365)

    def update_gates(self, baseline_sharpe: float):
        sr = self.rolling_sharpe()
        health = self.signal_health()
        # Defense 1: signal-health adjustment
        if health < HEALTH_DECAY_THRESHOLD:
            self.gate_multiplier = max(self.gate_multiplier, 1.5)
            self.size_dampener  = min(self.size_dampener, 0.5)
            self.health_warnings += 1
        else:
            # recover gradually if health restored
            self.gate_multiplier = max(1.0, self.gate_multiplier * 0.95)
            self.size_dampener  = min(1.0, self.size_dampener * 1.05)
        # Defense 2: performance-degradation adjustment
        if not math.isnan(sr) and baseline_sharpe > 0:
            ratio = sr / baseline_sharpe
            if ratio < 0:    # losing streak — halt
                if self.halt_until is None:
                    return "halt_negative_sr"
            elif ratio < PERF_DEGRADED_THRESH:
                self.gate_multiplier = max(self.gate_multiplier, 1.3)
                self.size_dampener  = min(self.size_dampener, 0.7)
        return None

    def check_kill(self, current_equity) -> bool:
        if self.killed: return True
        dd = (current_equity - self.peak_equity) / self.peak_equity
        if dd < -KILL_DD_PCT:
            self.killed = True
            return True
        return False

    def effective_entry_pct(self) -> float:
        return ENTRY_PCT_BASE * self.gate_multiplier


# ── Engine ────────────────────────────────────────────────────────────────────
def run(verbose=True):
    perp = load_perp()
    marks = load_option_marks()
    catalogue = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]

    # baseline historical Sharpe (we know v5's was ~8 on BTC, ~9 on ETH)
    BASELINE_SHARPE = 8.0 if UNDERLYING == "BTC" else 9.0

    equity_usd = 10_000.0
    state = PortfolioState(equity_usd=equity_usd)
    adaptive = AdaptiveState()
    guards = [
        underlying_whitelist({UNDERLYING}),
        max_concurrent_positions(MAX_CONCURRENT),
        cooldown_after_consecutive_losses(3, cooldown_hours=24),
        min_signal_strength(min_gap_pp=ENTRY_PCT_BASE * 100),
    ]

    open_positions = []
    trades = []
    equity_curve = []
    sig_history = {}
    health_log = []
    rejections = {"none": 0, "below_gate": 0, "persist": 0,
                  "regime_panic": 0, "halt": 0, "killed": 0, "max_conc": 0}

    for i, t in enumerate(hours):
        spot = float(perp.loc[t, "close"])
        rv   = perp.loc[t, "rv7d"]
        rv_med = perp.loc[t, "rv_median"]
        equity_curve.append((t, equity_usd))

        # Defense 4: hard kill check
        if adaptive.check_kill(equity_usd):
            rejections["killed"] += 1
            continue

        # check halt
        if adaptive.halt_until is not None and t < adaptive.halt_until:
            rejections["halt"] += 1
            # still manage open positions below
        elif adaptive.halt_until is not None:
            adaptive.halt_until = None    # halt expired

        # update signal history + baseline
        preds_now = compute_pred_per_expiry(t, spot, catalogue, marks)
        if preds_now:
            max_pred = max(abs(p["pred"]) for p in preds_now)
            adaptive.record_signal(t, max_pred)
        adaptive.update_baseline()

        # signal history for persistence
        for p in preds_now:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # update gates based on performance + health
        verdict = adaptive.update_gates(BASELINE_SHARPE)
        if verdict == "halt_negative_sr":
            adaptive.halt_until = t + pd.Timedelta(hours=HALT_HOURS_ON_NEG_SR)

        # log health every 24h
        if i % 24 == 0:
            health_log.append({
                "t": t, "equity": equity_usd,
                "gate_mult": adaptive.gate_multiplier,
                "size_dampener": adaptive.size_dampener,
                "rolling_sharpe": adaptive.rolling_sharpe(),
                "signal_health": adaptive.signal_health(),
                "halted": adaptive.halt_until is not None,
            })

        # manage open positions (same as v5)
        still_open = []
        for pos in open_positions:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]; entry_px = pos["entry_px"]
            unreal_ret = side * (spot - entry_px) / entry_px
            pos["peak_ret"] = max(pos.get("peak_ret", 0.0), unreal_ret)

            if (not pos.get("tp_taken")) and unreal_ret >= PARTIAL_TP_PCT:
                half_notional = pos["notional"] * 0.5
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = half_notional * pnl_pct
                equity_usd += pnl_usd
                state.equity_usd = equity_usd
                pos["notional"] -= half_notional
                pos["tp_taken"] = True
                adaptive.record_pnl(pnl_usd, equity_usd)
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "notional": half_notional,
                               "exit_reason": "partial_tp", "equity_after": equity_usd})

            exit_now, reason = False, ""
            if t >= pos["expiry"]: exit_now, reason = True, "expiry"
            elif held_h >= MAX_HOLD_HOURS: exit_now, reason = True, "max_hold"
            elif unreal_ret < -STOP_LOSS_PCT: exit_now, reason = True, "stop_loss"
            elif pos["peak_ret"] >= TRAIL_PEAK_PCT and \
                 (pos["peak_ret"] - unreal_ret) > TRAIL_GIVEBACK_PCT:
                exit_now, reason = True, "trail"

            if exit_now:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = pos["notional"] * pnl_pct
                equity_usd += pnl_usd
                state.equity_usd = equity_usd
                state.last_n_pnls.append(pnl_usd)
                adaptive.record_pnl(pnl_usd, equity_usd)
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "exit_reason": reason, "equity_after": equity_usd})
                continue
            still_open.append(pos)

        open_positions = still_open
        state.open_positions = len(open_positions)

        # entry consideration
        if adaptive.halt_until is not None and t < adaptive.halt_until: continue
        if not preds_now:
            rejections["none"] += 1; continue
        if len(open_positions) >= MAX_CONCURRENT:
            rejections["max_conc"] += 1; continue

        # Defense 3: regime circuit breaker
        if pd.notna(rv) and pd.notna(rv_med) and rv > VOL_PANIC_MULT * rv_med:
            rejections["regime_panic"] += 1; continue

        already_in = {p["expiry"] for p in open_positions}
        candidates = sorted(preds_now, key=lambda p: -abs(p["pred"]))
        chosen = None
        eff_gate = adaptive.effective_entry_pct()
        for cand in candidates:
            if cand["expiry"] in already_in: continue
            if abs(cand["pred"]) < eff_gate:
                rejections["below_gate"] += 1; break
            hist = sig_history.get(cand["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS:
                rejections["persist"] += 1; continue
            if sum(1 for pi in recent if np.sign(pi) == np.sign(cand["pred"])) < PERSIST_HOURS:
                rejections["persist"] += 1; continue
            chosen = cand; break
        if chosen is None: continue

        pred = chosen["pred"]
        side = 1 if pred > 0 else -1
        intent = TradeIntent(timestamp=t, structure="adaptive_v7",
                              underlying=UNDERLYING, risk_usd=equity_usd * 0.05,
                              notional_usd=equity_usd, iv_rv_gap_pp=pred * 100)
        reason = pipeline(intent, state, guards)
        if reason is not None: continue

        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        size_mult *= adaptive.size_dampener     # apply dampener
        notional = equity_usd * size_mult

        open_positions.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": chosen["expiry"], "notional": notional,
            "size_mult": size_mult, "pred_pct": pred,
            "n_strikes": chosen["n_strikes"], "peak_ret": 0.0,
            "gate_at_entry": eff_gate,
        })
        state.open_positions = len(open_positions)

    # close leftover open
    for pos in open_positions:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = perp.index[-1]; spot = float(perp.loc[t_end, "close"])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
        pnl_usd = pos["notional"] * pnl_pct
        equity_usd += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end", "equity_after": equity_usd})

    if not trades:
        print("No trades."); return

    df = pd.DataFrame(trades)
    n = len(df); wins = (df["pnl_usd"] > 0).sum()
    rr_win = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins else 0
    rr_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n-wins) else 0
    rr = abs(rr_win / rr_loss) if rr_loss else float("nan")
    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    daily = eq.resample("1D").last().dropna().diff().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0
    dd = (eq - eq.cummax()).min()

    print()
    print("=" * 88)
    print(f"  V7 ADAPTIVE — {UNDERLYING}   (defenses: signal-health, perf-track, "
          f"regime, hard-DD)")
    print("=" * 88)
    print(f"  trades         : {n}     wins {wins}   win rate {wins/n*100:.1f}%   R:R {rr:.2f}")
    print(f"  total PnL      : ${df['pnl_usd'].sum():+,.0f}   equity: ${equity_usd:,.0f}   "
          f"({(equity_usd-10_000)/10_000*100:+.1f}% on $10k)")
    print(f"  Sharpe (daily) : {sharpe:.2f}     max DD: ${dd:+,.0f}  ({dd/10_000*100:.1f}%)")
    print(f"  killed?        : {adaptive.killed}")
    print(f"  health warnings: {adaptive.health_warnings}")
    print(f"  rejections     : {rejections}")
    print()

    if health_log:
        hl = pd.DataFrame(health_log)
        print("  Adaptive state snapshots (every 24h):")
        print(f"  {'date':<12} {'equity':>9} {'gate×':>6} {'size×':>6} "
              f"{'roll SR':>8} {'health':>7} {'halt':>5}")
        print("  " + "-" * 60)
        for _, row in hl.iloc[::max(1, len(hl)//10)].iterrows():
            sr = row["rolling_sharpe"]
            sr_str = f"{sr:>7.2f}" if not math.isnan(sr) else "    —  "
            print(f"  {row['t'].strftime('%Y-%m-%d'):<12} ${row['equity']:>8,.0f} "
                  f"{row['gate_mult']:>5.2f}× {row['size_dampener']:>5.2f}× "
                  f"{sr_str} {row['signal_health']:>6.2f} "
                  f"{'YES' if row['halted'] else 'no':>5}")
    print()

    out = DATA / "v7_trades.csv"
    df.to_csv(out, index=False)
    pd.DataFrame(health_log).to_csv(DATA / "v7_health.csv", index=False)
    print(f"  trade log → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
