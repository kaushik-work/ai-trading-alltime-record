import sys; sys.path.insert(0,'.')
import numpy as np, pandas as pd
from nse.backtest.daily_journal import build, split_of
from nse.backtest.test_breakout_3stage import add_chop, run
from nse.backtest.test_breakout_retest import prepare
from nse.backtest.nifty_loader import load_spot

j = build()
bars = add_chop(prepare(load_spot()))
t = run(bars, 25.0, 5.0, three_stage=True, chop_min=1.0)
t["date"] = pd.to_datetime(t["date"])
m = t.merge(j, on="date", how="left")
m["split"] = m["date"].map(split_of)

# The four features whose sign held in TRAIN, VALID and TEST.
# All four favoured the LOW half, so we keep days below the TRAIN median.
CONS = ["or_range_pct", "pcr_vol", "call_wall_dist_pct", "total_oi_lakh"]
train = m[m["split"] == "TRAIN"]
thr = {f: train[f].median() for f in CONS}     # thresholds from TRAIN ONLY
print("Thresholds taken from TRAIN only (never from VALID/TEST):")
for f, v in thr.items():
    print(f"   {f:22} keep days <= {v:.2f}")

def report(sub, lab):
    if sub.empty:
        print(f"  {lab:34} no trades"); return
    cells = [sub[sub["split"] == k]["pts"].sum() for k in ("TRAIN","VALID","TEST")]
    ok = all(c > 0 for c in cells)
    print(f"  {lab:34}{len(sub):>7}{(sub['pts']>0).mean()*100:>6.0f}%"
          f"{sub['pts'].mean():>9.2f}" + "".join(f"{c:>10,.0f}" for c in cells)
          + f"{sub['pts'].sum():>10,.0f}" + ("   ALL +" if ok else ""))

print(f"\n  {'variant':34}{'trades':>7}{'WR':>7}{'pts/trd':>9}"
      f"{'TRAIN':>10}{'VALID':>10}{'TEST':>10}{'TOTAL':>10}")
report(m, "no journal filter (baseline)")
for f in CONS:
    report(m[m[f] <= thr[f]], f"filter: {f} <= med")
mask = np.ones(len(m), bool)
for f in CONS: mask &= (m[f] <= thr[f]).fillna(False).values
report(m[mask], "ALL FOUR combined")
for k in (2,3):
    cnt = sum((m[f] <= thr[f]).fillna(False).astype(int) for f in CONS)
    report(m[cnt >= k], f"at least {k} of 4")
