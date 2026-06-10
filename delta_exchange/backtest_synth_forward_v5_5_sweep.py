"""
Synthetic-Forward v5.5 — Trigger Frequency Research Sweep
==========================================================

Goal: figure out how to get MORE trades fired without giving up the v5 edge.
NOT using zone anchoring (per user request). All tweaks are signal-side
modifications to the synthetic-forward formula or expiry/strike selection.

Each variant runs the SAME exit logic as v5 (stop 1.5%, partial_tp 1%,
trail 0.25%, max_hold 72h). Only the ENTRY trigger varies.

Run:
    python backtest_synth_forward_v5_5_sweep.py             # BTC base data
    UNDERLYING=ETH python backtest_synth_forward_v5_5_sweep.py
    DATA_SUBDIR=oos_btc python backtest_synth_forward_v5_5_sweep.py

The output is a comparison table. Look for variants that increase trade
count substantially while preserving Sharpe + R:R close to baseline.
"""

from __future__ import annotations

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd


# ── data plumbing (mirrors v5) ───────────────────────────────────────────────
UNDERLYING  = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"
_data_override = os.environ.get("DATA_SUBDIR", "").strip()
if _data_override:
    DATA = Path(__file__).parent / "data" / _data_override
elif UNDERLYING == "BTC":
    DATA = Path(__file__).parent / "data"
else:
    DATA = Path(__file__).parent / "data" / UNDERLYING.lower()

PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
STOP_LOSS_PCT       = 0.015
PARTIAL_TP_PCT      = 0.010
TRAIL_PEAK_PCT      = 0.005
TRAIL_GIVEBACK_PCT  = 0.0025
MAX_HOLD_HOURS      = 72
MAX_CONCURRENT      = 2
MIN_STRIKES         = 3


def load_perp() -> pd.Series:
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def parse_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]; strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_option_marks() -> dict:
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(option_marks: dict) -> pd.DataFrame:
    rows = []
    for sym in option_marks:
        try: side, strike, expiry = parse_symbol(sym)
        except Exception: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


# ── variant definition ──────────────────────────────────────────────────────
@dataclass
class Variant:
    """One parameter set / formula variation to backtest.

    Each kwarg flips a specific signal-side behaviour; combine flags to test
    interactions. Exit logic + position sizing stay v5-identical so the only
    delta vs baseline is the trigger.
    """
    name: str
    gate: float           = 0.006   # |pred| threshold to enter
    persist_hours: int    = 2       # how long signal must hold above gate
    tt_min: float         = 6.0     # min hours-to-expiry
    tt_max: float         = 72.0    # max hours-to-expiry
    moneyness: float      = 0.05    # ±% around spot for strike inclusion

    # signal-construction variations (all default OFF = behaves like v5)
    composite_expiries: bool   = False  # signal = TTE-weighted avg across expiries
    consensus_bonus:    bool   = False  # if ≥80% strikes agree, gate × 0.5
    velocity_gate:      bool   = False  # use Δpred (1h) instead of |pred|
    rv_adaptive_gate:   bool   = False  # scale gate by 24h realized vol


def desc(v: Variant) -> str:
    bits = [f"gate {v.gate*100:.2f}%", f"persist {v.persist_hours}h",
            f"TTE {v.tt_min:.0f}-{v.tt_max:.0f}h"]
    if v.composite_expiries: bits.append("composite")
    if v.consensus_bonus:    bits.append("consensus-bonus")
    if v.velocity_gate:      bits.append("velocity")
    if v.rv_adaptive_gate:   bits.append("rv-adaptive")
    return " · ".join(bits)


# ── per-expiry signal compute ────────────────────────────────────────────────
def compute_pred_per_expiry(t: pd.Timestamp, spot: float,
                            catalogue: pd.DataFrame, marks: dict,
                            v: Variant) -> list[dict]:
    """Each eligible expiry → {expiry, pred, n_strikes, pos_strikes, neg_strikes,
    tte_hours}. pred is the median dislocation, the *_strikes counts feed the
    consensus-bonus variant."""
    tt_min = t + pd.Timedelta(hours=v.tt_min)
    tt_max = t + pd.Timedelta(hours=v.tt_max)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        common_near = [K for K in common if abs(K - spot) / spot <= v.moneyness]
        if len(common_near) < MIN_STRIKES: continue
        devs = []
        for K in common_near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            c_px = float(c.loc[t]); p_px = float(p.loc[t])
            if c_px <= 0 or p_px <= 0: continue
            devs.append(((c_px - p_px + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0)
        neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        tte_h = (exp - t).total_seconds() / 3600
        out.append({"expiry": exp, "pred": float(np.median(devs)),
                    "n_strikes": len(devs),
                    "pos_strikes": pos, "neg_strikes": neg,
                    "tte_hours": tte_h})
    return out


def pick_chosen_signal(preds_now: list[dict], v: Variant,
                       already_in: set, sig_history: dict,
                       t: pd.Timestamp, rv_24h: float,
                       rej: dict) -> Optional[dict]:
    """Decide whether to enter and which expiry to pick. Returns the chosen
    dict (with extra keys) or None — updates the rejection counters so we
    can see WHY a variant didn't fire."""

    if not preds_now:
        rej["none_eligible"] += 1
        return None

    # ── compose the effective signal source per variant ─────────────────────
    if v.composite_expiries:
        # TTE-weighted average across all expiries (closer expiry = more weight).
        # We then treat this composite as a single "virtual expiry" and attach
        # it to the highest-|pred| concrete expiry for routing.
        weights = np.array([1.0 / max(p["tte_hours"], 1.0) for p in preds_now])
        weighted_pred = float(np.sum(np.array([p["pred"] for p in preds_now]) * weights) / weights.sum())
        # Use the strongest concrete expiry as the routing target.
        target = max(preds_now, key=lambda p: abs(p["pred"]))
        candidates = [{**target, "pred": weighted_pred,
                       "effective_strikes": sum(p["n_strikes"] for p in preds_now)}]
    else:
        # standard v5: pick strongest first, fallback to next strongest
        candidates = sorted(preds_now, key=lambda p: -abs(p["pred"]))

    # ── effective gate (variant-aware) ──────────────────────────────────────
    base_gate = v.gate
    if v.rv_adaptive_gate and rv_24h > 0:
        # Scale gate proportionally to realized vol: high vol = wider gate to
        # avoid noise; low vol = tighter gate to capture subtle dislocations.
        # Anchor: median realized vol ≈ 2% / day on perp returns.
        scale = max(0.4, min(1.6, rv_24h / 0.02))
        base_gate = base_gate * scale

    for cand in candidates:
        if cand["expiry"] in already_in:
            continue

        # consensus bonus: if a clear majority of strikes lean the same way,
        # this is a corroborated signal — lower the gate by 50%.
        gate_for_cand = base_gate
        if v.consensus_bonus:
            total = cand["pos_strikes"] + cand["neg_strikes"]
            same  = cand["pos_strikes"] if cand["pred"] > 0 else cand["neg_strikes"]
            if total > 0 and same / total >= 0.80:
                gate_for_cand = base_gate * 0.5

        # velocity gate: instead of |pred| ≥ gate, require the CHANGE in pred
        # over the last hour to exceed gate. Catches regime shifts that
        # absolute-level gates miss.
        if v.velocity_gate:
            hist = sig_history.get(cand["expiry"], [])
            pred_1h_ago = None
            for ti, pi in reversed(hist):
                if (t - ti).total_seconds() >= 3600:
                    pred_1h_ago = pi
                    break
            if pred_1h_ago is None:
                rej["no_velocity_data"] += 1
                continue
            delta = cand["pred"] - pred_1h_ago
            if abs(delta) < gate_for_cand:
                rej["below_gate"] += 1
                continue
            # When velocity-gated, the entry side follows the DELTA direction.
            cand = {**cand, "side_hint": 1 if delta > 0 else -1}
        else:
            if abs(cand["pred"]) < gate_for_cand:
                rej["below_gate"] += 1
                if not v.composite_expiries:
                    # sorted by strength; lower ones won't pass either
                    return None
                continue

        # persistence check
        hist = sig_history.get(cand["expiry"], [])
        recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= v.persist_hours * 3600]
        if len(recent) < v.persist_hours:
            rej["no_persistence"] += 1
            continue
        sign_for = cand.get("side_hint") or (1 if cand["pred"] > 0 else -1)
        same_sign = sum(1 for pi in recent if np.sign(pi) == sign_for)
        if same_sign < v.persist_hours:
            rej["no_persistence"] += 1
            continue
        return cand
    return None


# ── backtest engine (per variant) ────────────────────────────────────────────
def run_variant(v: Variant, perp: pd.Series, marks: dict,
                catalogue: pd.DataFrame) -> dict:
    """Returns a metrics dict; exits/sizing are identical to v5."""
    equity_usd = 10_000.0
    open_positions: list = []
    trades: list = []
    equity_curve: list = [(perp.index[0], equity_usd)]
    sig_history: dict = {}
    rej = {"none_eligible": 0, "below_gate": 0, "no_persistence": 0,
           "no_velocity_data": 0}
    SIZE_BASE_PCT, SIZE_MIN, SIZE_MAX = 0.005, 0.5, 3.0

    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    # precompute 24h rolling realized vol for the rv-adaptive variant
    perp_ret = perp.pct_change()
    rv_24h = perp_ret.rolling("24h").std() * math.sqrt(60 * 24)

    for t in hours:
        spot = float(perp.loc[t])
        equity_curve.append((t, equity_usd))

        # ── update signal history per expiry ────────────────────────────────
        preds_now = compute_pred_per_expiry(t, spot, catalogue, marks, v)
        for p in preds_now:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # ── manage open positions ───────────────────────────────────────────
        still_open = []
        for pos in open_positions:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]; entry_px = pos["entry_px"]
            unreal_ret = side * (spot - entry_px) / entry_px
            pos["peak_ret"] = max(pos.get("peak_ret", 0.0), unreal_ret)
            if (not pos.get("tp_taken")) and unreal_ret >= PARTIAL_TP_PCT:
                half = pos["notional"] * 0.5
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = half * pnl_pct
                equity_usd += pnl_usd
                pos["notional"] -= half
                pos["tp_taken"] = True
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "notional": half, "exit_reason": "partial_tp"})
            exit_now, reason = False, ""
            if t >= pos["expiry"]:
                exit_now, reason = True, "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_now, reason = True, "max_hold"
            elif unreal_ret < -STOP_LOSS_PCT:
                exit_now, reason = True, "stop_loss"
            elif pos["peak_ret"] >= TRAIL_PEAK_PCT and \
                 (pos["peak_ret"] - unreal_ret) > TRAIL_GIVEBACK_PCT:
                exit_now, reason = True, "trail"
            if exit_now:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = pos["notional"] * pnl_pct
                equity_usd += pnl_usd
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "exit_reason": reason})
                continue
            still_open.append(pos)
        open_positions = still_open

        # ── entry consideration ─────────────────────────────────────────────
        if len(open_positions) >= MAX_CONCURRENT: continue
        already_in = {p["expiry"] for p in open_positions}
        rv_now = float(rv_24h.get(t, np.nan)) if not pd.isna(rv_24h.get(t, np.nan)) else 0.02
        chosen = pick_chosen_signal(preds_now, v, already_in, sig_history,
                                     t, rv_now, rej)
        if chosen is None: continue
        pred = chosen["pred"]
        side = chosen.get("side_hint") or (1 if pred > 0 else -1)
        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX, max(SIZE_MIN, abs(pred) / SIZE_BASE_PCT))
        open_positions.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": chosen["expiry"],
            "notional": equity_usd * size_mult,
            "size_mult": size_mult,
            "pred_pct": pred,
            "peak_ret": 0.0,
        })

    # ── final close-outs + metrics ──────────────────────────────────────────
    for pos in open_positions:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = perp.index[-1]; spot = float(perp.loc[t_end])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
        pnl_usd = pos["notional"] * pnl_pct
        equity_usd += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end"})

    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    if not trades:
        return {"variant": v.name, "n_trades": 0, "win_rate": 0,
                "rr": 0, "total_pct": 0, "sharpe": 0, "max_dd_pct": 0,
                "rej": rej, "trades_per_month": 0}
    df = pd.DataFrame(trades)
    wins = (df["pnl_usd"] > 0).sum()
    losses = (df["pnl_usd"] <= 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins  else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    total_pct = (eq.iloc[-1] - eq.iloc[0]) / eq.iloc[0] * 100
    dd_pct = (eq - eq.cummax()).min() / 10_000 * 100
    daily = eq.resample("1D").last().dropna().diff().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0
    span_months = (perp.index[-1] - perp.index[0]).total_seconds() / (30 * 86400)
    tpm = len(df) / max(span_months, 0.01)
    return {"variant": v.name, "n_trades": int(len(df)),
            "win_rate": float(wins / len(df) * 100),
            "rr": float(rr) if not math.isnan(rr) else 0.0,
            "total_pct": float(total_pct),
            "sharpe": float(sharpe),
            "max_dd_pct": float(dd_pct),
            "trades_per_month": float(tpm),
            "rej": rej}


# ── variant grid ─────────────────────────────────────────────────────────────
VARIANTS: list[Variant] = [
    Variant("baseline_v5"),
    # — gate sweep —
    Variant("gate_0.4%",                 gate=0.004),
    Variant("gate_0.3%",                 gate=0.003),
    Variant("gate_0.2%",                 gate=0.002),
    # — persistence sweep (keep 0.6% gate so we don't double-loosen) —
    Variant("persist_1h",                persist_hours=1),
    Variant("persist_3h",                persist_hours=3),
    # — TTE selection —
    Variant("short_tte_6-24h",           tt_min=6,  tt_max=24),
    Variant("mid_tte_24-48h",            tt_min=24, tt_max=48),
    Variant("long_tte_48-72h",           tt_min=48, tt_max=72),
    # — moneyness widening —
    Variant("moneyness_3%",              moneyness=0.03),
    Variant("moneyness_7%",              moneyness=0.07),
    # — formula tweaks —
    Variant("composite_expiries",        composite_expiries=True),
    Variant("consensus_bonus",           consensus_bonus=True),
    Variant("velocity_gate",             velocity_gate=True, gate=0.002),
    Variant("rv_adaptive_gate",          rv_adaptive_gate=True),
    # — combos —
    Variant("combo_0.4%_persist_1h",     gate=0.004, persist_hours=1),
    Variant("combo_0.3%_consensus",      gate=0.003, consensus_bonus=True),
    Variant("combo_short_tte_0.4%",      tt_min=6, tt_max=24, gate=0.004),
]


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print(f"Loading {UNDERLYING} data from {DATA}…")
    perp = load_perp()
    marks = load_option_marks()
    catalogue = build_index(marks)
    print(f"  perp bars: {len(perp):,}  options: {len(marks)}  "
          f"expiries: {catalogue['expiry'].nunique()}")
    span_d = (perp.index[-1] - perp.index[0]).days
    print(f"  span: {perp.index[0].date()} → {perp.index[-1].date()} ({span_d} days)")
    print()

    rows = []
    for v in VARIANTS:
        print(f"  running {v.name:<32} {desc(v)}", flush=True)
        m = run_variant(v, perp, marks, catalogue)
        rows.append(m)

    print()
    print("=" * 110)
    print(f"  {UNDERLYING}  Variant Comparison  ({span_d} days, $10k start, exit logic = v5)")
    print("=" * 110)
    print(f"  {'variant':<28} {'trades':>7} {'tpm':>6} {'WR%':>6} "
          f"{'R:R':>6} {'total%':>9} {'Sharpe':>7} {'maxDD%':>7}")
    print("  " + "─" * 96)
    base = next((r for r in rows if r["variant"] == "baseline_v5"), None)
    for r in rows:
        marker = ""
        if base and r["variant"] != "baseline_v5":
            if r["n_trades"] > base["n_trades"] * 1.5 and r["sharpe"] > base["sharpe"] * 0.7:
                marker = "  ⭐"   # more trades, edge preserved
            elif r["n_trades"] > base["n_trades"] and r["sharpe"] >= base["sharpe"]:
                marker = "  ✓"
        print(f"  {r['variant']:<28} {r['n_trades']:>7} {r['trades_per_month']:>6.1f} "
              f"{r['win_rate']:>6.1f} {r['rr']:>6.2f} {r['total_pct']:>+9.1f} "
              f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>+7.1f}{marker}")
    print()
    print("  ⭐ = ≥1.5× more trades than baseline AND Sharpe ≥ 70% of baseline")
    print("  ✓  = more trades than baseline AND Sharpe ≥ baseline")
    print()
    # rejection-count summary on baseline so we see WHY trades didn't fire
    if base:
        print(f"  Baseline rejection counters: {base['rej']}")


if __name__ == "__main__":
    main()
