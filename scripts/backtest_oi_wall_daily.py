"""
OI Wall Capture - Daily Timeframe Backtest.

Uses 120 days of overlap (bhavcopy + NIFTY daily OHLCV).
Signal: NIFTY daily close breaks OI wall → enter next day at open.
Exit: N days later or SL/TP using BS pricing.

More data, more trades, more statistical confidence.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import implied_vol, bs_price, days_to_expiry

parser = argparse.ArgumentParser()
parser.add_argument("--capital",    type=float, default=40_000)
parser.add_argument("--lots",       type=int,   default=3)
parser.add_argument("--slippage",   type=float, default=2.0, help="pts per side (daily = wider spread)")
parser.add_argument("--hold-days",  type=int,   default=3,   help="max days to hold (daily bars)")
parser.add_argument("--sl-pct",     type=float, default=0.40)
parser.add_argument("--tp-mult",    type=float, default=2.00)
parser.add_argument("--wall-prox",  type=int,   default=500)
parser.add_argument("--oi-mult",    type=float, default=1.2)
parser.add_argument("--pcr-lo-ce",  type=float, default=0.70)
parser.add_argument("--pcr-hi-pe",  type=float, default=1.50)
args = parser.parse_args()

CHAIN = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
D1    = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_1d.csv"
PCR_F = Path(__file__).parent.parent / "db" / "pcr_historical.csv"

chain = pd.read_csv(CHAIN); chain["date"] = pd.to_datetime(chain["date"]).dt.date
nifty = pd.read_csv(D1, index_col=0, parse_dates=True)
nifty.columns = [c.capitalize() for c in nifty.columns]
nifty.index   = pd.to_datetime(nifty.index, utc=True).tz_localize(None)
nifty["_date"] = nifty.index.date

pcr_hist = {}
if PCR_F.exists():
    p = pd.read_csv(PCR_F, usecols=["date","pcr_weekly"])
    pcr_hist = {r["date"]: float(r["pcr_weekly"]) for _, r in p.iterrows()}

LOT_SIZE = 65; QTY = args.lots * LOT_SIZE; SLIP = args.slippage
RISK_FREE = 0.065

chain_days = sorted(chain["date"].unique())
nifty_days = sorted(set(nifty["_date"]))
all_days   = sorted(set(chain_days) & set(nifty_days))
print(f"\nOI Wall Daily Backtest | {len(all_days)} overlap days | {all_days[0]} to {all_days[-1]}")


def atm_iv(dc, spot, exp, as_of):
    try:
        atm = int(round(spot / 50)) * 50
        row = dc[(dc["strike"] == atm) & (dc["option_type"] == "CE")]
        if row.empty: return 0.15
        px = float(row["settle"].iloc[0])
        if px <= 0: return 0.15
        T = days_to_expiry(date.fromisoformat(exp), today=as_of)
        if T <= 0: return 0.15
        iv = implied_vol(px, spot, atm, T, RISK_FREE, "CE")
        return iv if iv and 0.05 < iv < 2.0 else 0.15
    except: return 0.15


def find_walls(dc, spot):
    ce = dc[dc["option_type"] == "CE"]
    pe = dc[dc["option_type"] == "PE"]
    cw = pw = None
    if not ce.empty:
        near = ce[(ce["strike"] > spot) & (ce["strike"] <= spot + args.wall_prox)]
        if not near.empty:
            thr = float(ce["oi"].median()) * args.oi_mult
            act = near[near["oi"] > thr].sort_values("strike")
            if not act.empty: cw = int(act.iloc[0]["strike"])
    if not pe.empty:
        near = pe[(pe["strike"] < spot) & (pe["strike"] >= spot - args.wall_prox)]
        if not near.empty:
            thr = float(pe["oi"].median()) * args.oi_mult
            act = near[near["oi"] > thr].sort_values("strike", ascending=False)
            if not act.empty: pw = int(act.iloc[0]["strike"])
    return cw, pw


iv_cache = {}
for d in chain_days:
    dc = chain[chain["date"] == d]
    if dc.empty: continue
    spot = float(dc["spot"].median())
    exp  = dc["expiry"].iloc[0]
    iv_cache[d] = atm_iv(dc, spot, exp, d)


trades = []
equity = args.capital
position = None

print(f"{'Date':<12} {'Sig':<4} {'Wall':<6} {'Entry':>7} {'Exit':>7} {'PnL':>9}  Reason  PCR")
print("-" * 72)

i = 1
while i < len(all_days):
    d = all_days[i]

    if position:
        # Check exit condition for open position
        dc    = chain[chain["date"] == d]
        sigma = iv_cache.get(d, 0.15)
        row   = nifty[nifty["_date"] == d]
        if row.empty: i += 1; continue
        close = float(row["Close"].iloc[0])
        exp   = position["expiry"]
        curr  = max(bs_price(close, position["strike"], days_to_expiry(
                    date.fromisoformat(exp), today=d), RISK_FREE, sigma, position["opt"]) - SLIP, 0.5)

        sl_p   = position["entry_p"] * args.sl_pct
        tp_p   = position["entry_p"] * args.tp_mult
        days_h = (d - position["entry_date"]).days

        if curr <= sl_p:
            pnl = (max(sl_p - SLIP, 0.5) - position["entry_p"]) * QTY
            reason = "SL"
        elif curr >= tp_p:
            pnl = (min(tp_p + SLIP, tp_p * 1.05) - position["entry_p"]) * QTY
            reason = "TP"
        elif days_h >= args.hold_days:
            pnl = (curr - position["entry_p"]) * QTY
            reason = "TIME"
        else:
            i += 1; continue

        exit_p = position["entry_p"] + pnl / QTY
        trades.append({**position, "exit": round(exit_p, 1), "pnl": round(pnl, 2),
                       "reason": reason, "exit_date": str(d)})
        equity += pnl
        print(f"{d}  {position['opt']:<4} {position['wall']:<6} "
              f"Rs{position['entry_p']:>6.0f} Rs{exit_p:>6.0f} Rs{pnl:>+9.0f}  {reason}  {position['pcr']:.2f}")
        position = None
        i += 1
        continue

    # Signal: yesterday's chain vs today's NIFTY close
    prev_d = all_days[i - 1]
    prev_dc = chain[chain["date"] == prev_d]
    if prev_dc.empty: i += 1; continue

    prev_spot  = float(prev_dc["spot"].median())
    cw, pw     = find_walls(prev_dc, prev_spot)
    day_pcr    = pcr_hist.get(str(prev_d), 1.0)
    sigma      = iv_cache.get(prev_d, 0.15)
    exp        = prev_dc["expiry"].iloc[0]

    row = nifty[nifty["_date"] == d]
    if row.empty: i += 1; continue
    today_close = float(row["Close"].iloc[0])
    today_open  = float(row["Open"].iloc[0])

    # Signal fires on today's CLOSE; enter at TOMORROW's open
    if i + 1 >= len(all_days): i += 1; continue
    next_d  = all_days[i + 1]
    next_row = nifty[nifty["_date"] == next_d]
    if next_row.empty: i += 1; continue
    entry_spot = float(next_row["Open"].iloc[0])

    if cw and today_close > cw and args.pcr_lo_ce <= day_pcr <= 1.40:
        T  = days_to_expiry(date.fromisoformat(exp), today=next_d)
        ep = max(bs_price(entry_spot, cw, T, RISK_FREE, sigma, "CE") + SLIP, 1.0)
        position = {"opt": "CE", "wall": cw, "strike": cw, "entry_p": round(ep, 1),
                    "entry_spot": entry_spot, "entry_date": next_d,
                    "expiry": exp, "pcr": day_pcr, "sigma": sigma}
        i += 2; continue

    elif pw and today_close < pw and 0.65 <= day_pcr <= args.pcr_hi_pe:
        T  = days_to_expiry(date.fromisoformat(exp), today=next_d)
        ep = max(bs_price(entry_spot, pw, T, RISK_FREE, sigma, "PE") + SLIP, 1.0)
        position = {"opt": "PE", "wall": pw, "strike": pw, "entry_p": round(ep, 1),
                    "entry_spot": entry_spot, "entry_date": next_d,
                    "expiry": exp, "pcr": day_pcr, "sigma": sigma}
        i += 2; continue

    i += 1

print(f"\n{'='*72}")
if not trades:
    print("No trades. Relax --wall-prox or --oi-mult.")
else:
    df   = pd.DataFrame(trades)
    wins = df[df["pnl"] > 0]; losses = df[df["pnl"] <= 0]
    wr   = len(wins) / len(df) * 100
    gp   = wins["pnl"].sum(); gl = abs(losses["pnl"].sum())
    pf   = round(gp / gl, 2) if gl > 0 else 99.9
    net  = equity - args.capital
    print(f"Trades: {len(df)} | WR={wr:.1f}% | PF={pf} | Net Rs{net:+,.0f} ({net/args.capital*100:+.1f}%)")
    print(f"Avg win Rs{wins['pnl'].mean():+.0f}  |  Avg loss Rs{losses['pnl'].mean():+.0f}")
    print(f"Capital: Rs{args.capital:,.0f} -> Rs{equity:,.0f}")
    for r, g in df.groupby("reason"):
        wr_r = len(g[g["pnl"] > 0]) / len(g) * 100
        print(f"  {r:<6}: {len(g)} trades | PnL Rs{g['pnl'].sum():+,.0f} | WR {wr_r:.0f}%")
