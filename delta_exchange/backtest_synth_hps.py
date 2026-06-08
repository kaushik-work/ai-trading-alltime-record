"""
Synth-Forward v5 + HPS (High Probability Setup) Enhancement
============================================================
Adds three HPS concepts on top of the v5 parity signal:

  1. ZONE FILTER — only enter when spot is at/near a key hourly swing level
     (support for longs, resistance for shorts). Avoids mid-range entries.

  2. CANDLE CONFIRMATION — wait for a reversal candle at the zone:
       Long  → hammer or bullish marubozu  (close > open, small upper wick)
       Short → shooting star or bearish marubozu (close < open, small lower wick)

  3. ZONE-TO-ZONE EXITS
       First target  : 1:1 R:R (close 60% of position)
       Second target : next zone level (trail remaining 40% to zone)
       Dynamic stop  : swing high/low ± buffer (not fixed %)

Result: fewer trades, higher quality, better R:R.

Usage:
  .venv/Scripts/python backtest_synth_hps.py
  UNDERLYING=ETH .venv/Scripts/python backtest_synth_hps.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, os, re
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING  = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

# ── Signal config (same as v5) ────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.006"))
PERSIST_HOURS   = 1
MIN_STRIKES     = 3
MONEYNESS       = 0.05
TT_MIN_HOURS    = 6
TT_MAX_HOURS    = 72
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
SIZE_BASE_PCT   = 0.005
SIZE_MIN_MULT   = 0.5
SIZE_MAX_MULT   = 3.0
MAX_CONCURRENT  = 2
MAX_HOLD_HOURS  = 72

# ── HPS config ────────────────────────────────────────────────────────────────
ZONE_LOOKBACK       = 30    # bars to look back for swing highs/lows
ZONE_TOLERANCE      = 0.020 # 2.0% — how close to zone to allow entry (relaxed)
ZONE_BUFFER         = 0.004 # 0.4% buffer above/below zone for dynamic stop
CANDLE_BODY_MIN     = 0.001 # min body size (relaxed)
PARTIAL_CLOSE_FRAC  = 0.70  # close 70% at 1:1
START_EQUITY        = 10_000.0


# ── Data ──────────────────────────────────────────────────────────────────────
def load_perp_1m() -> pd.DataFrame:
    """Returns 1m DataFrame AND hourly OHLC aligned to exact-hour timestamps."""
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("timestamp").sort_index()
    return df


def resample_hourly(df: pd.DataFrame) -> pd.DataFrame:
    ohlc = df["close"].resample("1h").agg(
        open="first", high="max", low="min", close="last"
    ).dropna()
    return ohlc


def load_option_marks() -> dict:
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
        m = re.match(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, asset, strike, ddmmyy = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if asset != UNDERLYING: continue
        try:
            dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


# ── Signal (identical to v5) ──────────────────────────────────────────────────
def compute_signal(t, spot, catalogue, marks):
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    candidates = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            cs = marks.get(calls.loc[K, "symbol"])
            ps = marks.get(puts.loc[K, "symbol"])
            if cs is None or ps is None: continue
            if t not in cs.index or t not in ps.index: continue
            cp, pp = float(cs.loc[t]), float(ps.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0)
        neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        candidates.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    if not candidates: return None
    candidates.sort(key=lambda c: -abs(c["pred"]))
    best = candidates[0]
    if abs(best["pred"]) < ENTRY_PCT: return None
    return best


# ── HPS functions ─────────────────────────────────────────────────────────────
def get_zones(hourly: pd.DataFrame, t: pd.Timestamp, lookback: int = ZONE_LOOKBACK):
    """Return (support_levels, resistance_levels) from recent swing highs/lows."""
    hist = hourly.loc[:t].iloc[-lookback-1:-1]  # exclude current bar
    if len(hist) < 5:
        return [], []

    highs = hist["high"].values
    lows  = hist["low"].values

    resistances = []
    supports    = []
    for i in range(2, len(highs) - 2):
        # swing high: higher than 2 bars each side
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistances.append(highs[i])
        # swing low
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            supports.append(lows[i])

    return sorted(supports), sorted(resistances, reverse=True)


def is_at_zone(spot: float, supports: list, resistances: list,
               want_long: bool, tol: float = ZONE_TOLERANCE):
    """Check if spot is within tolerance of a support (long) or resistance (short)."""
    zones = supports if want_long else resistances
    for z in zones:
        if abs(spot - z) / spot <= tol:
            return True, z
    return False, None


def get_next_zone(spot: float, supports: list, resistances: list,
                  want_long: bool):
    """Find the next zone in the direction of trade (target zone)."""
    if want_long:
        # next resistance above spot
        above = [r for r in resistances if r > spot]
        return min(above) if above else None
    else:
        # next support below spot
        below = [s for s in supports if s < spot]
        return max(below) if below else None


def check_candle(hourly: pd.DataFrame, t: pd.Timestamp, want_long: bool):
    """
    Candle confirmation at zone.
    Long  → hammer (long lower wick, small upper wick) or bullish marubozu
    Short → shooting star (long upper wick, small lower wick) or bearish marubozu
    """
    if t not in hourly.index:
        return False
    bar = hourly.loc[t]
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    body   = abs(c - o)
    rng    = h - l
    if rng < 1e-6:
        return False

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_frac  = body / rng

    if want_long:
        # Hammer: lower wick >= 2× body, small upper wick, close > open
        hammer = (lower_wick >= 2 * body and upper_wick <= 0.3 * rng
                  and c > o)
        # Bullish marubozu: strong green, body > 60% of range
        marubozu = (c > o and body_frac > 0.6
                    and body / o > CANDLE_BODY_MIN)
        return hammer or marubozu
    else:
        # Shooting star: upper wick >= 2× body, small lower wick, close < open
        shooting = (upper_wick >= 2 * body and lower_wick <= 0.3 * rng
                    and c < o)
        # Bearish marubozu: strong red
        marubozu = (c < o and body_frac > 0.6
                    and body / o > CANDLE_BODY_MIN)
        return shooting or marubozu


def dynamic_stop(spot: float, zone_level: float, want_long: bool,
                 buffer: float = ZONE_BUFFER) -> float:
    """Stop = halfway between zone and spot (tighter than full zone buffer)."""
    if want_long:
        # stop halfway between zone support and spot, minus small buffer
        mid = (zone_level + spot) / 2
        return mid * (1 - buffer)
    else:
        mid = (zone_level + spot) / 2
        return mid * (1 + buffer)


# ── Backtest ──────────────────────────────────────────────────────────────────
def run():
    print(f"Loading {UNDERLYING} data...")
    perp_1m = load_perp_1m()
    hourly  = resample_hourly(perp_1m)
    marks   = load_option_marks()
    cat     = build_index(marks)
    hours   = hourly.index
    print(f"  perp 1h bars : {len(hourly):,}")
    print(f"  option marks : {len(marks):,} symbols")
    print(f"  decision pts : {len(hours):,}\n")

    equity       = START_EQUITY
    open_pos     = []
    trades       = []
    equity_curve = []
    sig_history  = {}
    rejections   = {"below_gate": 0, "no_persist": 0,
                    "no_zone": 0, "no_candle": 0, "max_conc": 0}

    sig_fired = 0
    for i, t in enumerate(hours):
        # use 1m price at this exact timestamp for signal; fallback to hourly close
        if t in perp_1m.index:
            spot = float(perp_1m.loc[t, "close"])
        else:
            spot = float(hourly.loc[t, "close"])
        equity_curve.append((t, equity))

        sig = compute_signal(t, spot, cat, marks)
        if sig:
            sig_fired += 1
            sig_history.setdefault(sig["expiry"], []).append((t, sig["pred"]))
        for exp in list(sig_history):
            sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                if (t - ti).total_seconds() <= 6 * 3600]

        # manage open positions
        still_open = []
        for pos in open_pos:
            side      = pos["side"]
            entry_px  = pos["entry_px"]
            notional  = pos["notional"]
            stop_px   = pos["stop_px"]
            target1   = pos["target1"]      # 1:1 target
            target2   = pos["target2"]      # zone-to-zone target

            unreal_ret = side * (spot - entry_px) / entry_px
            held_h     = (t - pos["entry_t"]).total_seconds() / 3600

            # partial close at 1:1 target
            if not pos.get("tp_taken") and target1 and \
               ((side == 1 and spot >= target1) or (side == -1 and spot <= target1)):
                partial_n  = notional * PARTIAL_CLOSE_FRAC
                fill_px    = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret        = side * (fill_px - entry_px) / entry_px
                pnl_usd    = partial_n * (ret - 2 * PERP_FEE_BPS / 1e4)
                equity    += pnl_usd
                pos["notional"] -= partial_n
                pos["tp_taken"]  = True
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_usd": pnl_usd,
                               "exit_reason": "partial_tp_1to1",
                               "notional": partial_n,
                               "equity_after": equity})

            notional  = pos["notional"]
            exit_reason = None
            if t >= pos["expiry"]:
                exit_reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif side == 1  and spot <= stop_px:
                exit_reason = "stop_loss"
            elif side == -1 and spot >= stop_px:
                exit_reason = "stop_loss"
            elif target2 and ((side == 1 and spot >= target2) or
                              (side == -1 and spot <= target2)):
                exit_reason = "zone_target"

            if exit_reason:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret     = side * (fill_px - entry_px) / entry_px
                pnl_usd = notional * (ret - 2 * PERP_FEE_BPS / 1e4)
                equity += pnl_usd
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_usd": pnl_usd,
                               "exit_reason": exit_reason,
                               "equity_after": equity})
            else:
                still_open.append(pos)

        open_pos = still_open

        # entry
        if len(open_pos) >= MAX_CONCURRENT:
            rejections["max_conc"] += 1
            continue
        if sig is None:
            continue

        already_in = {p["expiry"] for p in open_pos}
        if sig["expiry"] in already_in:
            continue

        # persistence
        hist   = sig_history.get(sig["expiry"], [])
        recent = [p for ti, p in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent) < PERSIST_HOURS:
            rejections["no_persist"] += 1
            continue
        if sum(1 for p in recent if np.sign(p) == np.sign(sig["pred"])) < PERSIST_HOURS:
            rejections["no_persist"] += 1
            continue

        want_long = sig["pred"] > 0
        side      = 1 if want_long else -1

        # HPS Zone filter
        supports, resistances = get_zones(hourly, t)
        at_zone, zone_lvl = is_at_zone(spot, supports, resistances, want_long)
        if not at_zone:
            rejections["no_zone"] += 1
            continue

        # HPS Candle confirmation
        if not check_candle(hourly, t, want_long):
            rejections["no_candle"] += 1
            continue

        # compute targets and dynamic stop
        stop_px = dynamic_stop(spot, zone_lvl, want_long)
        risk    = abs(spot - stop_px)
        target1 = spot + side * risk            # 1:1
        target2 = get_next_zone(spot, supports, resistances, want_long)

        fill_px   = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(sig["pred"]) / SIZE_BASE_PCT))
        notional  = equity * size_mult

        open_pos.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": sig["expiry"], "notional": notional,
            "size_mult": size_mult, "pred_pct": sig["pred"],
            "n_strikes": sig["n_strikes"],
            "stop_px": stop_px, "target1": target1, "target2": target2,
            "zone_lvl": zone_lvl,
        })

    # close remaining
    for pos in open_pos:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = hours[-1]; spot = float(hourly.iloc[-1]["close"])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_usd = pos["notional"] * (ret - 2 * PERP_FEE_BPS / 1e4)
        equity += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end", "equity_after": equity})

    print(f"  Signals fired (above gate): {sig_fired}")
    print(f"  Rejections: {rejections}")
    if not trades:
        print("No trades passed all filters.")
        return

    df = pd.DataFrame(trades)
    df["exit_t"] = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n    = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0,  "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n-wins) else 0
    rr   = abs(avg_win / avg_loss) if avg_loss else float("nan")
    eq   = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    dd   = (eq - eq.cummax()).min()
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0

    print()
    print("=" * 80)
    print(f"  Synth-Forward v5 + HPS Zones ({UNDERLYING}, gate {ENTRY_PCT*100:.1f}%)")
    print(f"  Zone filter + Candle confirmation + Zone-to-zone exits")
    print("=" * 80)
    print(f"  Trades      : {n}  (wins {wins}  losses {n-wins}  win rate {wins/n*100:.1f}%)")
    print(f"  Avg win/loss: ${avg_win:+,.0f} / ${avg_loss:+,.0f}   R:R {rr:.2f}")
    print(f"  Total PnL   : ${df['pnl_usd'].sum():+,.0f}")
    print(f"  Final equity: ${equity:,.0f}  ({(equity-START_EQUITY)/START_EQUITY*100:+.1f}%)")
    print(f"  Sharpe      : {sharpe:.2f}   Max DD: ${dd:+,.0f}  ({dd/START_EQUITY*100:.1f}%)")
    print()

    monthly = df.groupby(df["exit_t"].dt.to_period("M"))["pnl_usd"].agg(["sum","count"])
    print("  Monthly breakdown:")
    for m, row in monthly.iterrows():
        print(f"    {m}  trades={int(row['count']):>3}  pnl=${row['sum']:>+8,.0f}")
    print()

    print("  Exit reasons:")
    for reason, grp in df.groupby("exit_reason"):
        wr = (grp["pnl_usd"] > 0).mean() * 100
        print(f"    {reason:<18} {len(grp):>3} trades  win {wr:>5.1f}%  "
              f"total ${grp['pnl_usd'].sum():>+8,.0f}")

    print(f"\n  Rejections: {rejections}")
    print(f"\n  vs v5 benchmark: 86 trades  +116%  Sharpe 8.82  win 88.4%")
    out = DATA / "hps_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log -> {out}")


if __name__ == "__main__":
    run()
