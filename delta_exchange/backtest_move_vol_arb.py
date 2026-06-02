"""
MOVE Option Daily Vol Arbitrage
================================
MOVE option payoff: |S_settle − K|. Mathematically a packaged straddle —
pure long-volatility instrument with no directional view, just vol view.

Strategy:
  At each MOVE listing (~24h before expiry):
    1. Compute MOVE-implied vol from current mark price (Brenner-Subrahmanyam
       inversion: MOVE_price ≈ S × σ × sqrt(2T/π))
    2. Compute trailing 24h realized vol of BTCUSD perp
    3. Take position based on RV vs IV gap:
       RV > IV + GATE → LONG MOVE  (buy underpriced vol)
       IV > RV + GATE → SHORT MOVE (sell overpriced vol)
    4. Hold to expiry. Settle at |S_settle − K|.

Costs:
  OPT_FEE_BPS = 25 per side
  Slippage: 50 bps round-trip (MOVE spreads are wider)
"""

import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
MOVE_DIR = DATA / "move"
PERP_FILE = DATA / "perp" / "BTCUSD_mark_1m.csv"

# Dials
RV_LOOKBACK_HOURS = 24
GATE_PP           = 5.0       # |IV - RV| must exceed this many vol points
ENTRY_FRAC_PCT    = 0.04      # 4% of equity per MOVE position (max loss = premium for long)
SHORT_SIZE_FRAC   = 0.02      # 2% of equity at risk for short (capped by max move)
OPT_FEE_BPS       = 25.0
SLIPPAGE_BPS      = 25.0      # one-side, applied entry + exit
MIN_TT_HOURS      = 2         # need at least 2h to expiry to enter
MAX_TT_HOURS      = 36


def parse_move_symbol(sym):
    """MV-BTC-71400-020626 → ('BTC', 71400, datetime(2026, 6, 2, 12))"""
    m = re.match(r"MV-(\w+)-(\d+)-(\d{6})", sym)
    if not m: return None
    asset, strike, ddmmyy = m.group(1), int(m.group(2)), m.group(3)
    dd, mm, yy = ddmmyy[:2], ddmmyy[2:4], ddmmyy[4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return asset, strike, expiry


def load_perp():
    df = pd.read_csv(PERP_FILE)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def load_move_marks():
    """Returns list of dicts: {symbol, strike, expiry, mark_series}"""
    out = []
    for p in sorted(MOVE_DIR.glob("*_mark_15m.csv")):
        sym = p.name.replace("_mark_15m.csv", "")
        parsed = parse_move_symbol(sym)
        if not parsed: continue
        asset, strike, expiry = parsed
        if asset != "BTC": continue
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        mark = df.set_index("timestamp")["close"].sort_index()
        out.append({"symbol": sym, "strike": strike,
                    "expiry": expiry, "mark": mark})
    return out


def move_implied_vol(move_price, S, T_yrs):
    """Invert MOVE ≈ S × σ × sqrt(2T/π) for σ."""
    if T_yrs <= 0 or move_price <= 0 or S <= 0: return float("nan")
    return move_price / S / math.sqrt(2 * T_yrs / math.pi)


def realized_vol(perp, t, lookback_hours):
    end = t
    start = t - pd.Timedelta(hours=lookback_hours)
    sub = perp.loc[start:end]
    if len(sub) < 10: return float("nan")
    logret = np.log(sub).diff().dropna()
    if len(logret) < 5 or logret.std() == 0: return float("nan")
    return float(logret.std() * math.sqrt(365 * 24 * 60))


def run():
    print("Loading data...")
    perp = load_perp()
    moves = load_move_marks()
    print(f"  perp 1m: {len(perp):,} bars")
    print(f"  MOVE contracts: {len(moves)}")
    print()

    equity = 10_000.0
    trades = []

    for m in moves:
        K = m["strike"]; expiry = m["expiry"]
        mark = m["mark"]
        if mark.empty: continue
        # entry = first available timestamp in usable TT window
        eligible = mark[(mark.index >= expiry - pd.Timedelta(hours=MAX_TT_HOURS)) &
                         (mark.index <= expiry - pd.Timedelta(hours=MIN_TT_HOURS))]
        if eligible.empty: continue
        t_entry = eligible.index[0]
        move_px = float(eligible.iloc[0])
        if move_px <= 0: continue
        T_yrs = (expiry - t_entry).total_seconds() / (365 * 86400)
        # spot at entry
        if t_entry in perp.index:
            S0 = float(perp.loc[t_entry])
        else:
            ix = perp.index.get_indexer([t_entry], method="nearest")[0]
            S0 = float(perp.iloc[ix])
        # implied vol from MOVE
        iv = move_implied_vol(move_px, S0, T_yrs)
        # realized vol
        rv = realized_vol(perp, t_entry, RV_LOOKBACK_HOURS)
        if not (np.isfinite(iv) and np.isfinite(rv)): continue
        gap_pp = (iv - rv) * 100

        # entry decision
        side = None     # +1 long MOVE (buy vol), -1 short MOVE (sell vol)
        if rv > iv + GATE_PP / 100:
            side = +1
            fee_basis = ENTRY_FRAC_PCT
        elif iv > rv + GATE_PP / 100:
            side = -1
            fee_basis = SHORT_SIZE_FRAC
        else:
            continue   # no edge

        # settle at expiry
        if expiry in perp.index:
            S_exp = float(perp.loc[expiry])
        else:
            ix = perp.index.get_indexer([expiry], method="nearest")[0]
            S_exp = float(perp.iloc[ix])
        settled_price = abs(S_exp - K)
        # fill prices include slippage
        fill_entry = move_px * (1 + side * SLIPPAGE_BPS / 1e4)
        fill_exit  = settled_price * (1 - side * SLIPPAGE_BPS / 1e4)
        # number of contracts: budget / entry premium
        budget = equity * fee_basis
        n_contracts = budget / max(fill_entry, 1e-6)
        entry_fee = n_contracts * fill_entry * OPT_FEE_BPS / 1e4
        exit_fee = n_contracts * fill_exit * OPT_FEE_BPS / 1e4
        # PnL: long MOVE = (settled - paid); short MOVE = (received - paid)
        pnl_per_contract = side * (fill_exit - fill_entry)
        pnl_usd = n_contracts * pnl_per_contract - entry_fee - exit_fee
        equity += pnl_usd

        trades.append({
            "entry_t": t_entry, "expiry": expiry,
            "K": K, "S0": S0, "S_exp": S_exp,
            "move_px_entry": move_px, "move_px_exit": settled_price,
            "iv_pct": iv * 100, "rv_pct": rv * 100, "gap_pp": gap_pp,
            "side": "LONG" if side == 1 else "SHORT",
            "n_contracts": n_contracts, "notional_budget": budget,
            "pnl_usd": pnl_usd, "equity_after": equity,
        })

    if not trades:
        print("No trades produced. Try lowering GATE_PP or check MOVE coverage.")
        return

    df = pd.DataFrame(trades).sort_values("expiry").reset_index(drop=True)
    n = len(df); wins = (df["pnl_usd"] > 0).sum()
    avg_win = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n - wins) else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")

    print("=" * 84)
    print(f"  MOVE Vol-Arb (BTC) — daily horizon, gate {GATE_PP}pp IV vs RV")
    print("=" * 84)
    print(f"  trades   : {n}     wins {wins}   win rate {wins/n*100:.1f}%   R:R {rr:.2f}")
    print(f"  avg win  : ${avg_win:+,.0f}   avg loss ${avg_loss:+,.0f}")
    print(f"  total PnL: ${df['pnl_usd'].sum():+,.0f}   equity ${equity:,.0f}   "
          f"({(equity - 10_000)/10_000*100:+.1f}% on $10k)")
    print()
    print(f"  Long-MOVE: {(df['side']=='LONG').sum()} trades   "
          f"Short-MOVE: {(df['side']=='SHORT').sum()} trades")
    print()
    print("  By side:")
    for side_lbl in ["LONG", "SHORT"]:
        sub = df[df["side"] == side_lbl]
        if sub.empty: continue
        print(f"    {side_lbl:<6} n={len(sub):>3}  win {(sub['pnl_usd']>0).mean()*100:.1f}%  "
              f"avg ${sub['pnl_usd'].mean():+.0f}  total ${sub['pnl_usd'].sum():+,.0f}")
    print()

    out = DATA / "move_vol_arb_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
