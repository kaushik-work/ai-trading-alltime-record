"""
OI Wall Capture Strategy Backtest.

Core logic:
  Morning setup (from PREVIOUS day bhavcopy):
    CE wall = nearest strike ABOVE spot with above-median CE OI (within WALL_PROX)
    PE wall = nearest strike BELOW spot with above-median PE OI (within WALL_PROX)

  Entry (9:30-11:30, 5m bar):
    BUY CE  - NIFTY close crosses ABOVE ce_wall (fresh break, not already above)
    BUY PE  - NIFTY close crosses BELOW pe_wall (fresh break)
    Filter  - PCR < 1.40 for CE, PCR > 0.65 for PE

  Option pricing at each bar - Black-Scholes with prev-day ATM IV
    SL = option drops to 40% of entry premium
    TP = option reaches 200% of entry premium (1:2 RR on risk)
    Time exit = 11:30 hard square-off at current BS price

Usage:
  python scripts/backtest_oi_wall.py --lots 3 --slippage 1 --capital 40000
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import implied_vol, bs_price, days_to_expiry

parser = argparse.ArgumentParser()
parser.add_argument("--capital",    type=float, default=40_000)
parser.add_argument("--lots",       type=int,   default=3)
parser.add_argument("--slippage",   type=float, default=1.0)
parser.add_argument("--wall-prox",  type=int,   default=400,  help="max pts from spot to qualify as wall")
parser.add_argument("--oi-mult",    type=float, default=1.2,  help="OI must exceed N*median to be a wall")
parser.add_argument("--sl-pct",     type=float, default=0.40, help="Hard SL: option drops to this fraction of entry (backup only)")
parser.add_argument("--tp-mult",    type=float, default=2.00, help="TP when option reaches this multiple of entry")
parser.add_argument("--iv-rank-max",type=float, default=75,   help="skip if IV rank > this (0-100)")
parser.add_argument("--pcr-lo-ce",  type=float, default=0.70, help="Min PCR to enter CE (below = bullish herd, avoid)")
parser.add_argument("--pcr-hi-pe",  type=float, default=1.50, help="Max PCR to enter PE (above = bearish herd, avoid)")
args = parser.parse_args()

CHAIN_FILE  = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
NIFTY_5M    = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_180d.csv"
PCR_FILE    = Path(__file__).parent.parent / "db" / "pcr_historical.csv"
LOT_SIZE    = 65
CAPITAL     = args.capital
LOTS        = args.lots
QTY         = LOTS * LOT_SIZE
SLIP        = args.slippage
WALL_PROX   = args.wall_prox
OI_MULT     = args.oi_mult
SL_PCT      = args.sl_pct        # hard backup SL (option drops to this fraction of entry)
TP_MULT     = args.tp_mult       # TP when option reaches this multiple of entry
IV_MAX      = args.iv_rank_max / 100.0
PCR_LO_CE   = args.pcr_lo_ce    # CE entry only when PCR > this (avoids bullish herd)
PCR_HI_PE   = args.pcr_hi_pe    # PE entry only when PCR < this (avoids bearish herd)
RISK_FREE   = 0.065
START_H, START_M = 9, 30
EXIT_H,  EXIT_M  = 15, 20


# ── Load data ────────────────────────────────────────────────────────────────

chain = pd.read_csv(CHAIN_FILE)
chain["date"] = pd.to_datetime(chain["date"]).dt.date

nifty5 = pd.read_csv(NIFTY_5M, index_col=0, parse_dates=True)
nifty5.columns = [c.capitalize() for c in nifty5.columns]
nifty5.index = pd.to_datetime(nifty5.index, utc=True).tz_localize(None)
nifty5["_date"] = nifty5.index.date

pcr_hist = {}
if PCR_FILE.exists():
    pcr_df  = pd.read_csv(PCR_FILE, usecols=["date", "pcr_weekly"])
    pcr_hist = {r["date"]: float(r["pcr_weekly"]) for _, r in pcr_df.iterrows()}

trading_days = sorted(chain["date"].unique())
overlap_days = sorted(set(nifty5["_date"]) & set(trading_days))


# ── IV from bhavcopy (Black-Scholes back-solve) ───────────────────────────────

def atm_iv_from_chain(day_chain: pd.DataFrame, spot: float,
                      expiry_str: str, as_of: date) -> float:
    """Back-calculate IV from ATM settlement price. Must pass historical date."""
    try:
        atm    = int(round(spot / 50)) * 50
        ce_row = day_chain[(day_chain["strike"] == atm) & (day_chain["option_type"] == "CE")]
        if ce_row.empty:
            return 0.0
        px = float(ce_row["settle"].iloc[0])
        if px <= 0:
            return 0.0
        T  = days_to_expiry(date.fromisoformat(expiry_str), today=as_of)
        if T <= 0:
            return 0.0
        iv = implied_vol(px, spot, atm, T, RISK_FREE, "CE")
        return iv if iv and 0.05 < iv < 2.0 else 0.0
    except Exception:
        return 0.0


iv_by_date: dict[date, float] = {}
for d in trading_days:
    dc   = chain[chain["date"] == d]
    spot = float(dc["spot"].median())
    exp  = dc["expiry"].iloc[0] if not dc.empty else ""
    iv_by_date[d] = atm_iv_from_chain(dc, spot, exp, as_of=d)

def iv_rank(d: date) -> float:
    """IV rank of today vs past 30 days with valid IV."""
    past  = [iv_by_date[dd] for dd in sorted(iv_by_date) if dd < d and iv_by_date.get(dd, 0) > 0][-30:]
    today = iv_by_date.get(d, 0)
    if len(past) < 5 or today <= 0:
        return 0.5
    lo, hi = min(past), max(past)
    return (today - lo) / (hi - lo) if hi > lo else 0.5


# ── Wall detection ────────────────────────────────────────────────────────────

def find_walls(day_chain: pd.DataFrame, spot: float):
    ce = day_chain[day_chain["option_type"] == "CE"]
    pe = day_chain[day_chain["option_type"] == "PE"]
    ce_wall = pe_wall = None

    if not ce.empty:
        nearby = ce[(ce["strike"] > spot) & (ce["strike"] <= spot + WALL_PROX)]
        if not nearby.empty:
            threshold = float(ce["oi"].median()) * OI_MULT
            active    = nearby[nearby["oi"] > threshold].sort_values("strike")
            if not active.empty:
                ce_wall = int(active.iloc[0]["strike"])

    if not pe.empty:
        nearby = pe[(pe["strike"] < spot) & (pe["strike"] >= spot - WALL_PROX)]
        if not nearby.empty:
            threshold = float(pe["oi"].median()) * OI_MULT
            active    = nearby[nearby["oi"] > threshold].sort_values("strike", ascending=False)
            if not active.empty:
                pe_wall = int(active.iloc[0]["strike"])

    return ce_wall, pe_wall


# ── Option price at a given spot using Black-Scholes ─────────────────────────

def option_price(spot: float, strike: int, opt: str,
                 expiry_str: str, trade_date: date, sigma: float) -> float:
    """BS price for the option at current bar."""
    try:
        T  = days_to_expiry(date.fromisoformat(expiry_str), today=trade_date)
        if T <= 0:
            intrinsic = max(0, spot - strike) if opt == "CE" else max(0, strike - spot)
            return max(intrinsic, 0.5)
        return max(bs_price(spot, strike, T, RISK_FREE, sigma, opt), 0.5)
    except Exception:
        return 0.5


# ── Backtest loop ─────────────────────────────────────────────────────────────

trades   = []
equity   = CAPITAL

print(f"\n{'='*75}")
print(f"  OI Wall Capture Backtest")
print(f"  Capital Rs{CAPITAL:,.0f} | {LOTS} lots ({QTY} units) | slip={SLIP}pt")
print(f"  SL={SL_PCT*100:.0f}% of entry | TP={TP_MULT:.0f}x entry | IV rank max={IV_MAX*100:.0f}%")
print(f"{'='*75}")
print(f"  {'Date':<12} {'Sig':<4} {'Wall':<6} {'Entry':>7} {'Exit':>7} {'PnL':>9}  Reason  IV_Rank  PCR")
print(f"  {'-'*75}")

for i, d in enumerate(overlap_days):
    if i == 0:
        continue

    prev_d      = overlap_days[i - 1]
    prev_chain  = chain[chain["date"] == prev_d]
    today_chain = chain[chain["date"] == d]

    if prev_chain.empty or today_chain.empty:
        continue

    prev_spot   = float(prev_chain["spot"].median())
    expiry_str  = today_chain["expiry"].iloc[0]

    # Use prev day's IV for pricing (approximate)
    sigma = iv_by_date.get(prev_d, 0)
    if sigma <= 0:
        sigma = 0.15   # fallback 15% if IV not available

    # IV rank filter — skip if options too expensive
    rank = iv_rank(d)
    if rank > IV_MAX:
        continue

    # PCR from previous day
    day_pcr = pcr_hist.get(str(prev_d), 1.0)

    ce_wall, pe_wall = find_walls(prev_chain, prev_spot)
    if ce_wall is None and pe_wall is None:
        continue

    # 5m intraday loop
    bars     = nifty5[nifty5["_date"] == d]
    position = None
    prev_close = None
    entered  = False   # one trade per day

    for ts, row in bars.iterrows():
        h, m   = ts.hour, ts.minute
        bar_min = h * 60 + m
        s_min   = START_H * 60 + START_M
        e_min   = EXIT_H  * 60 + EXIT_M
        price   = float(row["Close"])

        # EOD hard exit
        if bar_min >= e_min and position:
            curr_p = option_price(price, position["strike"], position["opt"],
                                  expiry_str, d, sigma) - SLIP
            curr_p = max(curr_p, 0.5)
            pnl    = (curr_p - position["entry_p"]) * QTY
            trades.append({
                "date": str(d), "opt": position["opt"], "wall": position["wall"],
                "entry": position["entry_p"], "exit": curr_p, "pnl": round(pnl, 2),
                "reason": "TIME", "iv_rank": rank, "pcr": day_pcr,
            })
            equity += pnl
            print(f"  {d}  {position['opt']:<4} {position['wall']:<6} "
                  f"Rs{position['entry_p']:>6.0f} Rs{curr_p:>6.0f} Rs{pnl:>+9.0f}  "
                  f"TIME    {rank:.2f}   {day_pcr:.2f}")
            position = None
            break

        if bar_min < s_min:
            prev_close = price
            continue
        if bar_min >= e_min:
            break

        # Manage open position with BS pricing
        if position:
            curr_p = option_price(price, position["strike"], position["opt"],
                                  expiry_str, d, sigma)
            hard_sl_p = position["entry_p"] * SL_PCT   # absolute backstop only
            tp_p      = position["entry_p"] * TP_MULT

            # SL: option drops below hard floor OR NIFTY is 2×wall_prox against us
            hard_reversal = (
                (position["opt"] == "CE" and price < position["wall"] - 50) or
                (position["opt"] == "PE" and price > position["wall"] + 50)
            )
            sl_hit = hard_reversal or curr_p <= hard_sl_p

            if sl_hit:
                exit_p = max(curr_p - SLIP, 0.5)
                pnl    = (exit_p - position["entry_p"]) * QTY
                reason = "SL-REV" if hard_reversal else "SL-PCT"
                trades.append({
                    "date": str(d), "opt": position["opt"], "wall": position["wall"],
                    "entry": position["entry_p"], "exit": exit_p, "pnl": round(pnl, 2),
                    "reason": reason, "iv_rank": rank, "pcr": day_pcr,
                })
                equity += pnl
                print(f"  {d}  {position['opt']:<4} {position['wall']:<6} "
                      f"Rs{position['entry_p']:>6.0f} Rs{exit_p:>6.0f} Rs{pnl:>+9.0f}  "
                      f"{reason:<8} {rank:.2f}   {day_pcr:.2f}")
                position = None
            elif curr_p >= tp_p:
                exit_p = max(tp_p - SLIP, 0.5)
                pnl    = (exit_p - position["entry_p"]) * QTY
                trades.append({
                    "date": str(d), "opt": position["opt"], "wall": position["wall"],
                    "entry": position["entry_p"], "exit": exit_p, "pnl": round(pnl, 2),
                    "reason": "TP", "iv_rank": rank, "pcr": day_pcr,
                })
                equity += pnl
                print(f"  {d}  {position['opt']:<4} {position['wall']:<6} "
                      f"Rs{position['entry_p']:>6.0f} Rs{exit_p:>6.0f} Rs{pnl:>+9.0f}  "
                      f"TP      {rank:.2f}   {day_pcr:.2f}")
                position = None
            prev_close = price
            continue

        # Entry: CE wall break — PCR must be in range (not bullish herd, not extreme bear)
        if (not entered and ce_wall and prev_close is not None
                and prev_close <= ce_wall < price
                and PCR_LO_CE <= day_pcr <= 1.40):
            ep = option_price(price, ce_wall, "CE", expiry_str, d, sigma) + SLIP
            position = {"opt": "CE", "wall": ce_wall, "strike": ce_wall,
                        "entry_p": ep, "entry_spot": price}
            entered = True

        # Entry: PE wall break — PCR must be in range (not bearish herd, not extreme bull)
        elif (not entered and pe_wall and prev_close is not None
                and prev_close >= pe_wall > price
                and 0.65 <= day_pcr <= PCR_HI_PE):
            ep = option_price(price, pe_wall, "PE", expiry_str, d, sigma) + SLIP
            position = {"opt": "PE", "wall": pe_wall, "strike": pe_wall,
                        "entry_p": ep, "entry_spot": price}
            entered = True

        prev_close = price


# ── Results ───────────────────────────────────────────────────────────────────

print(f"\n{'='*75}")
if not trades:
    print("  No trades. Try: --wall-prox 500 --oi-mult 1.0")
    sys.exit(0)

df = pd.DataFrame(trades)
wins   = df[df["pnl"] > 0]
losses = df[df["pnl"] <= 0]
wr     = len(wins) / len(df) * 100
gp     = wins["pnl"].sum()
gl     = abs(losses["pnl"].sum())
pf     = round(gp / gl, 2) if gl > 0 else float("inf")
net    = equity - CAPITAL
net_pct = net / CAPITAL * 100

print(f"  RESULTS - OI Wall Capture Strategy")
print(f"  {len(df)} trades | CE={len(df[df['opt']=='CE'])} | PE={len(df[df['opt']=='PE'])}")
print(f"  WR={wr:.1f}%  |  PF={pf}  |  Net Rs{net:+,.0f} ({net_pct:+.1f}%)")
print(f"  Avg win Rs{wins['pnl'].mean():+.0f}  |  Avg loss Rs{losses['pnl'].mean():+.0f}")
print(f"  Capital: Rs{CAPITAL:,.0f} -> Rs{equity:,.0f}")
print()
for reason, grp in df.groupby("reason"):
    wr_r = len(grp[grp["pnl"] > 0]) / len(grp) * 100
    print(f"  {reason:<6}: {len(grp)} trades | PnL Rs{grp['pnl'].sum():+,.0f} | WR {wr_r:.0f}%")
print(f"\n  Avg IV rank at entry: {df['iv_rank'].mean():.2f}")
print(f"  Avg PCR at entry    : {df['pcr'].mean():.2f}")
