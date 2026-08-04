"""Option-data coverage check for backtesting options on price-action signals.

Research-only diagnostic. Reproduces the V0 price-action S/R signal stream
(wick_touch, tol 0.0007, body_pos 0.70, SL 0.6% BTC / 0.7% ETH, 1:7, cooldown 60,
block-after-loss 180, 24h vol filter <=34% ETH only) by importing
backtest_price_action_sweep, then checks whether the collected Delta option
1m CSVs cover the contracts needed to backtest option structures on those
entries.

Usage (cwd = delta_exchange):
    ./.venv/Scripts/python.exe check_option_coverage.py

Planned structures evaluated per signal:
    (A) debit buy ATM / ATM+-1 step  -> 6 contracts (C+P at ATM-1, ATM, ATM+1)
    (B) debit spread buy ATM + sell ATM+2 steps -> 4 contracts (C+P at ATM, ATM+2)

Verdict rule: if a structure is fully covered (file exists + 1m mark within
2 min of entry + series reaches entry+4h) for >= 80% of signals, recommend
real-marks-only; otherwise recommend a Black-Scholes fallback and report the
fraction of trades that would need model pricing.
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

import backtest_price_action_sweep as hp

DATA = Path(__file__).parent / "data"

ENTRY_MARK_TOL_S = 120          # 1m row within +-2 min of entry counts as entry mark
MAX_HOLD_H = 4                  # position-management window (MAX_HOLD_CANDLES=240)
MIN_DTE_DAYS = 2.0              # nearest expiry with >= 2 days to expiry
VERDICT_THRESHOLD = 0.80

# V0 live dials
ASSETS = {
    "BTC": dict(sym="BTCUSD", sl_pct=0.006, rr=7, vol_filter_max=0.0),
    "ETH": dict(sym="ETHUSD", sl_pct=0.007, rr=7, vol_filter_max=0.34),
}
RUN_KW = dict(
    use_trend=True, trail_be=True,
    retest_mode="wick_touch", body_pos_threshold=0.70, wick_touch_tol=0.0007,
    min_volume_mult=1.0, rsi_period=14, rsi_long_max=100, rsi_short_min=0,
    cooldown_candles=60, block_after_loss_candles=180,
)

FILE_RE = re.compile(r"([CP])-([A-Z]+)-(\d+)-(\d{6})_mark_(1m|1h)\.csv")


# ---------------------------------------------------------------- data index

def parse_option_filename(name: str):
    m = FILE_RE.match(name)
    if not m:
        return None
    side, strike, d, gran = m.group(1), int(m.group(3)), m.group(4), m.group(5)
    expiry = pd.Timestamp(f"20{d[4:6]}-{d[2:4]}-{d[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry, gran


def build_option_index(asset: str):
    """{(side, strike, expiry): (path, granularity)} + strike-step inference."""
    opt_dir = DATA / asset.lower() / "options"
    index = {}
    strikes = set()
    expiries = set()
    unparsed = []
    for f in sorted(opt_dir.glob("*.csv")):
        p = parse_option_filename(f.name)
        if p is None:
            unparsed.append(f.name)
            continue
        side, strike, expiry, gran = p
        index[(side, strike, expiry)] = (f, gran)
        strikes.add(strike)
        expiries.add(expiry)
    ss = sorted(strikes)
    diffs = Counter(b - a for a, b in zip(ss, ss[1:]))
    step = diffs.most_common(1)[0][0] if diffs else None
    return index, sorted(expiries), step, diffs, unparsed


class OptionSeriesCache:
    """Lazy per-file timestamp loader (only the `time` column is needed)."""

    def __init__(self):
        self._cache: dict[Path, np.ndarray] = {}

    def times(self, path: Path) -> np.ndarray:
        if path not in self._cache:
            df = pd.read_csv(path, usecols=["time"])
            self._cache[path] = np.sort(df["time"].values.astype(np.int64))
        return self._cache[path]

    def max_gap_min(self, path: Path, t0: int, t1: int) -> float:
        ts = self.times(path)
        w = ts[(ts >= t0) & (ts <= t1)]
        if len(w) < 2:
            return np.nan
        return float(np.max(np.diff(w)) / 60.0)


# ---------------------------------------------------------------- signals

def get_signals(asset: str, cfg: dict):
    """Reproduce the V0 signal stream via the sweep harness."""
    kw = dict(RUN_KW, vol_filter_max=cfg["vol_filter_max"])
    trades, equity, curve = hp.run_asset(
        asset.lower(), cfg["sym"], cfg["sl_pct"], cfg["rr"], **kw)
    sigs = [{"entry_time": t["entry_time"], "side": t["side"], "entry": t["entry"]}
            for t in trades]
    return sigs


def load_perp_close(asset: str, sym: str) -> pd.Series:
    df = pd.read_csv(DATA / asset.lower() / "perp" / f"{sym}_mark_1m.csv",
                     usecols=["time", "close"])
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


# ---------------------------------------------------------------- checking

def check_contract(cache: OptionSeriesCache, index, key, entry_ts: pd.Timestamp):
    """Return (status, max_gap_min). status in ok / missing_file / no_entry_mark /
    truncated_window."""
    if key not in index:
        return "missing_file", np.nan
    path, gran = index[key]
    ts = cache.times(path)
    if len(ts) == 0:
        return "missing_file", np.nan
    t0 = int(entry_ts.timestamp())
    t1 = t0 + MAX_HOLD_H * 3600
    # entry mark within +-2 min
    i = np.searchsorted(ts, t0)
    near = []
    for j in (i - 1, i):
        if 0 <= j < len(ts):
            near.append(abs(int(ts[j]) - t0))
    if not near or min(near) > ENTRY_MARK_TOL_S:
        return "no_entry_mark", np.nan
    # series continues through entry + 4h (row at/after window end, 2-min grace)
    if ts[-1] < t1 - ENTRY_MARK_TOL_S:
        return "truncated_window", np.nan
    gap = cache.max_gap_min(path, t0, t1)
    return "ok", gap


def dte_bucket(days: float) -> str:
    if days < 3:
        return "2-3d"
    if days < 8:
        return "4-7d"
    if days < 15:
        return "8-14d"
    return ">14d"


def analyse_asset(asset: str, cfg: dict):
    index, expiries, step, step_diffs, unparsed = build_option_index(asset)
    perp = load_perp_close(asset, cfg["sym"])
    sigs = get_signals(asset, cfg)
    cache = OptionSeriesCache()

    print("=" * 96)
    print(f"{asset} — {len(sigs)} V0 signals | option files indexed: {len(index)}"
          f" | expiries: {len(expiries)} | inferred strike step: {step}"
          f" (diff histogram: {dict(step_diffs.most_common(5))})")
    if unparsed:
        print(f"  unparsed filenames: {unparsed}")
    if not sigs:
        print("  no signals — nothing to check")
        return None

    structures = {
        "A_debit_ATM_pm1": lambda atm: [(s, atm + k * step)
                                        for s in ("C", "P") for k in (-1, 0, 1)],
        "B_debit_spread_ATM_plus2": lambda atm: [(s, atm + k * step)
                                                 for s in ("C", "P") for k in (0, 2)],
    }

    # strikes collected per expiry (side-agnostic; C and P share the strike grid here)
    strikes_by_expiry: dict[pd.Timestamp, list[int]] = defaultdict(list)
    for (s, k, e) in index:
        if s == "C":
            strikes_by_expiry[e].append(k)
    strikes_by_expiry = {e: sorted(v) for e, v in strikes_by_expiry.items()}

    rows = []
    quirks_gap = []       # (signal idx, key, gap_min) for ok contracts with gaps
    for si, sig in enumerate(sigs):
        entry_ts = sig["entry_time"]
        # spot at entry (exact perp index hit expected; nearest within 2 min fallback)
        try:
            spot = float(perp.loc[entry_ts])
        except KeyError:
            near = perp.reindex([entry_ts], method="nearest",
                                tolerance=pd.Timedelta(minutes=2))
            spot = float(near.iloc[0]) if not near.isna().iloc[0] else np.nan
        atm = int(np.floor(spot / step + 0.5) * step) if np.isfinite(spot) else None

        # nearest expiry with >= MIN_DTE_DAYS to expiry
        exp = None
        for e in expiries:
            if (e - entry_ts).total_seconds() >= MIN_DTE_DAYS * 86400:
                exp = e
                break
        dte_days = ((exp - entry_ts).total_seconds() / 86400) if exp is not None else np.nan

        row = {"idx": si, "entry_time": entry_ts, "side": sig["side"],
               "spot": spot, "atm": atm, "expiry": exp, "dte_days": dte_days,
               "month": entry_ts.strftime("%Y-%m"),
               "dte_bucket": dte_bucket(dte_days) if np.isfinite(dte_days) else "none"}
        if exp is None or atm is None:
            for name in structures:
                row[name] = "no_expiry" if exp is None else "no_spot"
            row["atm_pair"] = row["A_debit_ATM_pm1"]
            row["relaxed_pair"] = row["A_debit_ATM_pm1"]
            row["relaxed_gap_pct"] = np.nan
            rows.append(row)
            continue
        for name, legs_fn in structures.items():
            statuses = []
            for s, k in legs_fn(atm):
                st, gap = check_contract(cache, index, (s, k, exp), entry_ts)
                statuses.append(st)
                if st == "ok" and np.isfinite(gap) and gap > 5:
                    quirks_gap.append((si, f"{s}-{k}-{exp:%d%m%y}", gap))
            if all(st == "ok" for st in statuses):
                row[name] = "ok"
            else:
                # dominant failure cause by precedence
                for cause in ("missing_file", "no_entry_mark", "truncated_window"):
                    if cause in statuses:
                        row[name] = cause
                        break
            row[name + "_legs_ok"] = sum(st == "ok" for st in statuses)
            row[name + "_legs_total"] = len(statuses)
            row[name + "_files_missing"] = sum(st == "missing_file" for st in statuses)

        # exact ATM C+P pair only
        pair_statuses = [check_contract(cache, index, (s, atm, exp), entry_ts)[0]
                         for s in ("C", "P")]
        row["atm_pair"] = ("ok" if all(st == "ok" for st in pair_statuses)
                           else next((c for c in ("missing_file", "no_entry_mark",
                                                  "truncated_window") if c in pair_statuses),
                                     "ok"))

        # moneyness-relaxed: nearest collected strike for this expiry, C+P
        avail = strikes_by_expiry.get(exp, [])
        if avail:
            k_near = min(avail, key=lambda x: abs(x - spot))
            row["relaxed_gap_pct"] = abs(k_near - spot) / spot * 100
            rel_statuses = [check_contract(cache, index, (s, k_near, exp), entry_ts)[0]
                            for s in ("C", "P")]
            row["relaxed_pair"] = ("ok" if all(st == "ok" for st in rel_statuses)
                                   else next((c for c in ("missing_file", "no_entry_mark",
                                                          "truncated_window")
                                              if c in rel_statuses), "ok"))
        else:
            row["relaxed_gap_pct"] = np.nan
            row["relaxed_pair"] = "missing_file"
        rows.append(row)

    df = pd.DataFrame(rows)

    # ------------------------------------------------ per-structure report
    for name in structures:
        legs_total = int(df[name + "_legs_total"].iloc[0]) if name + "_legs_total" in df else 6
        print(f"\n  Structure {name} ({legs_total} contracts/signal)")
        files_ok = df[name].isin(["ok", "no_entry_mark", "truncated_window"])
        entry_ok = df[name].isin(["ok", "truncated_window"])
        full_ok = df[name] == "ok"
        print(f"    signals total:            {len(df)}")
        print(f"    all contract files exist: {files_ok.mean() * 100:5.1f}%")
        print(f"    all entry-time marks:     {entry_ok.mean() * 100:5.1f}%")
        print(f"    full 4h window marks:     {full_ok.mean() * 100:5.1f}%")
        causes = df[name].value_counts()
        print(f"    failure breakdown: {dict(causes)}")
        cov = full_ok.mean()
        if cov >= VERDICT_THRESHOLD:
            print(f"    VERDICT: coverage {cov * 100:.1f}% >= 80% -> real-marks-only "
                  f"(drop the {(1 - cov) * 100:.1f}% uncovered signals)")
        else:
            print(f"    VERDICT: coverage {cov * 100:.1f}% < 80% -> Black-Scholes fallback needed; "
                  f"{(1 - cov) * 100:.1f}% of trades would be model-priced "
                  f"(perp mark + IV interpolated from nearest option marks)")

    # Exact ATM C+P pair and moneyness-relaxed nearest-strike pair
    for col, label in [("atm_pair", "exact ATM C+P pair"),
                       ("relaxed_pair", "nearest collected strike C+P (moneyness-relaxed)")]:
        full_ok = df[col] == "ok"
        print(f"\n  Reference — {label}:")
        print(f"    full 4h window marks:     {full_ok.mean() * 100:5.1f}%"
              f"  (failures: {dict(df[col].value_counts().drop('ok', errors='ignore'))})")
        if col == "relaxed_pair":
            g = df.loc[full_ok, "relaxed_gap_pct"].dropna()
            if len(g):
                print(f"    |strike - spot| at entry for covered signals (% of spot): "
                      f"median {g.median():.2f}%, p90 {g.quantile(0.9):.2f}%, max {g.max():.2f}%")
        cov = full_ok.mean()
        verdict = ("real-marks-only" if cov >= VERDICT_THRESHOLD else "BS fallback")
        print(f"    VERDICT: coverage {cov * 100:.1f}% -> {verdict}")

    # ------------------------------------------------ breakdowns
    def pivot(group_col):
        print(f"\n  Coverage by {group_col} (% signals fully covered, structure A / structure B):")
        print(f"    {group_col:10} {'n':>4} {'A ok%':>7} {'B ok%':>7}")
        for g, sub in sorted(df.groupby(group_col)):
            a = (sub["A_debit_ATM_pm1"] == "ok").mean() * 100
            b = (sub["B_debit_spread_ATM_plus2"] == "ok").mean() * 100
            print(f"    {str(g):10} {len(sub):>4} {a:>6.1f}% {b:>6.1f}%")

    pivot("month")
    pivot("dte_bucket")

    # ------------------------------------------------ quirks
    print("\n  Data quirks:")
    dtes = df["dte_days"].dropna()
    if len(dtes):
        print(f"    DTE of chosen expiry: min {dtes.min():.2f}d, median {dtes.median():.2f}d, "
              f"max {dtes.max():.2f}d (nearest expiry >= {MIN_DTE_DAYS:.0f}d rule)")
    exp_counts = Counter(df["expiry"].dropna())
    top_exp = exp_counts.most_common(5)
    print(f"    most-used expiries: {[(f'{e:%Y-%m-%d}', n) for e, n in top_exp]}")
    atm_hits = df[df["A_debit_ATM_pm1"].isin(["ok", "no_entry_mark", "truncated_window"])]
    print(f"    signals where ATM+-1 files all exist: {len(atm_hits)}/{len(df)}")
    if quirks_gap:
        worst = sorted(quirks_gap, key=lambda x: -x[2])[:5]
        print(f"    1m-series gaps > 5 min inside hold window: {len(quirks_gap)} contract-windows; "
              f"worst: {[(k, round(g, 1)) for _, k, g in worst]}")
    else:
        print("    no >5 min gaps inside hold windows of covered contracts")
    if asset == "ETH":
        kw_off = dict(RUN_KW, vol_filter_max=0.0)
        n_off = len(hp.run_asset(asset.lower(), cfg["sym"], cfg["sl_pct"], cfg["rr"],
                                 **kw_off)[0])
        print(f"    24h vol filter <=34% cuts ETH signals from {n_off} to {len(df)} "
              f"(vol_filter_max=0.0 for comparison)")
    return df


def main():
    print("Option-data coverage for price-action V0 signals (real-mark backtest feasibility)")
    print(f"entry-mark tolerance +/-{ENTRY_MARK_TOL_S}s | hold window {MAX_HOLD_H}h | "
          f"min DTE {MIN_DTE_DAYS:.0f}d | verdict threshold {VERDICT_THRESHOLD * 100:.0f}%")
    results = {}
    for asset, cfg in ASSETS.items():
        results[asset] = analyse_asset(asset, cfg)
    print("\n" + "=" * 96)
    print("Summary verdicts")
    for asset, df in results.items():
        if df is None:
            continue
        for name, label in [("A_debit_ATM_pm1", "A: debit buy ATM/ATM+-1 (6 legs)"),
                            ("B_debit_spread_ATM_plus2", "B: debit spread ATM/ATM+2 (4 legs)"),
                            ("atm_pair", "ref: exact ATM C+P pair"),
                            ("relaxed_pair", "ref: nearest collected strike C+P")]:
            cov = (df[name] == "ok").mean() * 100
            v = "real-marks-only" if cov >= VERDICT_THRESHOLD * 100 else "BS fallback"
            print(f"  {asset} {label:40} coverage {cov:5.1f}% -> {v}")


if __name__ == "__main__":
    main()
