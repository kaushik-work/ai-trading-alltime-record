import sys; sys.path.insert(0,'.')
import numpy as np, pandas as pd
import nse.backtest.breakout_options_ledger as L
from nse.backtest.test_breakout_retest import prepare
from nse.backtest.nifty_loader import load_spot
from nse.backtest.daily_journal import build, split_of

bars = prepare(load_spot())
print(f"LOT = {L.LOT} (from config)   BUDGET = Rs {L.BUDGET:,.0f}   band Rs {L.PREMIUM_LO:.0f}-{L.PREMIUM_HI:.0f}\n")

sig = L.index_signals(bars, 25.0, 5.0)
rows=[]
for _, s in sig.iterrows():
    ch = L.cached_chain(s["date"])
    if ch is None: continue
    wc = s["dir"] > 0
    fin  = pd.Timestamp(s["entry_dt"]) + L.BAR
    fout = pd.Timestamp(s["exit_dt"])  + L.BAR
    p = L.pick_strike(ch, fin, wc)
    if p is None: continue
    strike, ent = p
    lots = int(L.BUDGET // (ent * L.LOT))
    if lots < 1: continue
    ex = L.premium_at(ch, fout, strike, wc)
    if ex is None: continue
    qty = lots*L.LOT
    held = (fout-fin).total_seconds()/60
    rows.append({"date":pd.Timestamp(s["date"]),"side":"CE" if wc else "PE","strike":int(strike),
                 "entry":ent,"exit":ex,"lots":lots,"deployed":ent*qty,
                 "pnl":(ex-ent)*qty,"opt_pts":ex-ent,
                 "idx_pts":s["dir"]*(s["exit_idx"]-s["entry_idx"]),
                 "held_min":held,"reason":s["reason"]})
t = pd.DataFrame(rows)
t["split"] = t["date"].map(split_of)
t["realised_delta"] = t["opt_pts"]/t["idx_pts"].replace(0,np.nan)

print("=== ACTUAL RECORDED PRICES — no delta assumption ===")
w,l = t[t.pnl>0], t[t.pnl<=0]
print(f"  {len(t)} trades   WR {len(w)/len(t)*100:.1f}%   avg deployed Rs {t.deployed.mean():,.0f}")
print(f"  {'':10}{'n':>5}{'idx pts':>10}{'opt pts':>10}{'Rs/trade':>12}{'held min':>10}")
for lab,g in (("WINNER",w),("LOSER",l),("ALL",t)):
    print(f"  {lab:10}{len(g):>5}{g.idx_pts.mean():>10.1f}{g.opt_pts.mean():>10.1f}"
          f"{g.pnl.mean():>12,.0f}{g.held_min.mean():>10.0f}")
print(f"\n  TOTAL Rs {t.pnl.sum():,.0f}")
for k in ("TRAIN","VALID","TEST"):
    g=t[t.split==k]
    if len(g): print(f"    {k:6} {len(g):>4} trades  Rs {g.pnl.sum():>10,.0f}")

print("\n=== The delta assumption, checked against reality ===")
rd = t["realised_delta"].replace([np.inf,-np.inf],np.nan).dropna()
rd = rd[(rd>-3)&(rd<3)]
print(f"  assumed 0.50   |   realised median {rd.median():.2f}  mean {rd.mean():.2f}  p25 {rd.quantile(.25):.2f}  p75 {rd.quantile(.75):.2f}")
print(f"  winners median {t[t.pnl>0]['realised_delta'].median():.2f}   losers median {t[t.pnl<=0]['realised_delta'].median():.2f}")
