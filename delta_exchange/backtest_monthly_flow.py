"""
Monthly Flow / Calendar Effect Strategy
========================================
Exploits recurring institutional flow patterns at month boundaries.

Original strategy (from @quantscience_) tested on TLT bonds:
  SHORT: enter day 1 of month, exit day 5
  LONG:  enter 7 days before EOM, exit 1 day before EOM

Hypothesis for BTC:
  - Start of month: new capital flows IN (bullish bias)
  - End of month: portfolio rebalancing, profit-taking (mixed)

We test multiple calendar configurations on BTC and ETH (2018-2026)
using Yahoo Finance data, then show the best signal.

Assets tested:
  BTC-USD, ETH-USD (crypto)
  TLT (original - bonds, for reference)

Usage:
  .venv/Scripts/python backtest_monthly_flow.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

START = "2018-01-01"
END   = "2026-06-01"
CAPITAL = 10_000.0
FEE_BPS = 10.0   # 0.10% round trip (crypto perp)


# ── Data ──────────────────────────────────────────────────────────────────────
def fetch(ticker: str) -> pd.Series:
    df = yf.download(ticker, start=START, end=END, progress=False, auto_adjust=True)
    close = df["Close"].squeeze().dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


# ── Calendar signal builder ───────────────────────────────────────────────────
def build_signals(close: pd.Series,
                  short_dom: int = 1,   # day-of-month to go SHORT
                  short_hold: int = 5,  # hold short N days
                  long_days_before_eom: int = 7,  # go LONG N days before EOM
                  long_hold: int = 6,   # hold long N days
                  ) -> pd.DataFrame:
    """
    Returns DataFrame with columns: long_entry, long_exit, short_entry, short_exit
    All boolean Series aligned to close.index.
    """
    idx = close.index
    dom = pd.Series(idx.day, index=idx)                    # day of month
    eom = pd.Series(idx, index=idx).resample("ME").transform("last")
    days_to_eom = (eom - idx).dt.days

    sig = pd.DataFrame(index=idx, data={
        "long_entry":  False,
        "long_exit":   False,
        "short_entry": False,
        "short_exit":  False,
    })

    # SHORT: enter on day `short_dom` of each month, exit `short_hold` days later
    short_entry_mask = (dom == short_dom)
    sig.loc[short_entry_mask, "short_entry"] = True
    sig["short_exit"] = sig["short_entry"].shift(short_hold).fillna(False)

    # LONG: enter `long_days_before_eom` before EOM, exit `long_hold` days later
    long_entry_mask = (days_to_eom == long_days_before_eom)
    sig.loc[long_entry_mask, "long_entry"] = True
    sig["long_exit"] = sig["long_entry"].shift(long_hold).fillna(False)

    return sig


# ── Simple vectorised backtest ────────────────────────────────────────────────
def backtest(close: pd.Series, sig: pd.DataFrame) -> pd.DataFrame:
    """
    State machine: track position (+1 long, -1 short, 0 flat).
    P&L = position × daily return minus fees on every flip.
    """
    pos    = pd.Series(0, index=close.index, dtype=float)
    state  = 0

    for i in range(1, len(close)):
        prev = state
        # process exits first
        if state == 1  and sig["long_exit"].iloc[i]:  state = 0
        if state == -1 and sig["short_exit"].iloc[i]: state = 0
        # then entries (only if flat)
        if state == 0:
            if sig["long_entry"].iloc[i]:  state =  1
            if sig["short_entry"].iloc[i]: state = -1
        pos.iloc[i] = state

    daily_ret = close.pct_change().fillna(0)
    turnover  = pos.diff().abs().fillna(0)
    gross     = pos.shift(1).fillna(0) * daily_ret
    cost      = turnover * FEE_BPS / 1e4
    net       = gross - cost

    equity = (1 + net).cumprod() * CAPITAL
    bm     = (1 + daily_ret).cumprod() * CAPITAL

    res = pd.DataFrame({
        "close": close, "pos": pos, "daily_ret": daily_ret,
        "gross": gross, "cost": cost, "net": net,
        "equity": equity, "benchmark": bm,
    })
    return res


# ── Metrics ───────────────────────────────────────────────────────────────────
def metrics(res: pd.DataFrame) -> dict:
    net = res["net"]
    eq  = res["equity"]
    bm  = res["benchmark"]

    ann_ret  = (eq.iloc[-1] / CAPITAL) ** (252 / len(eq)) - 1
    bm_ret   = (bm.iloc[-1] / CAPITAL) ** (252 / len(bm)) - 1
    daily_std = net.std()
    sharpe   = net.mean() / daily_std * math.sqrt(252) if daily_std > 0 else 0
    dd       = (eq - eq.cummax()) / eq.cummax()
    max_dd   = dd.min()
    calmar   = ann_ret / abs(max_dd) if max_dd != 0 else 0

    trades   = (res["pos"].diff().abs() > 0).sum() // 2
    in_pos   = (res["pos"] != 0)
    win_rate = (net[in_pos] > 0).mean() if in_pos.sum() > 0 else 0

    monthly  = eq.resample("ME").last().pct_change().dropna()
    pos_months = (monthly > 0).sum()
    neg_months = (monthly <= 0).sum()

    return {
        "total_return"  : (eq.iloc[-1] - CAPITAL) / CAPITAL,
        "ann_return"    : ann_ret,
        "bm_return"     : bm_ret,
        "sharpe"        : sharpe,
        "calmar"        : calmar,
        "max_dd"        : max_dd,
        "win_rate"      : win_rate,
        "trades"        : int(trades),
        "pos_months"    : int(pos_months),
        "neg_months"    : int(neg_months),
    }


# ── Parameter sweep ───────────────────────────────────────────────────────────
def sweep(close: pd.Series, ticker: str) -> pd.DataFrame:
    """Test multiple parameter combinations and return ranked results."""
    configs = [
        # (short_dom, short_hold, long_days_before_eom, long_hold, label)
        (1,  5,  7, 6,  "Original (TLT)"),
        (1,  5,  7, 6,  "Original"),
        (1,  3,  7, 6,  "Short-3d"),
        (1,  5,  5, 4,  "Long-5d-before"),
        (1,  5,  3, 2,  "Long-3d-before"),
        (2,  4,  7, 5,  "Short-day2"),
        (1,  7,  7, 6,  "Short-7d"),
        (1,  5,  10, 8, "Long-10d-before"),
        (None, None, 7, 6,  "Long-only"),
        (1,  5,  None, None, "Short-only"),
    ]

    rows = []
    for cfg in configs:
        sd, sh, ld, lh, lbl = cfg
        sig = build_signals(close,
                            short_dom=sd or 99,
                            short_hold=sh or 1,
                            long_days_before_eom=ld or 99,
                            long_hold=lh or 1)
        if sd is None:
            sig["short_entry"] = False; sig["short_exit"] = False
        if ld is None:
            sig["long_entry"] = False; sig["long_exit"] = False

        res = backtest(close, sig)
        m   = metrics(res)
        rows.append({"config": lbl, **m, "_res": res})

    return pd.DataFrame(rows)


# ── Print ─────────────────────────────────────────────────────────────────────
def print_sweep(df: pd.DataFrame, ticker: str) -> None:
    print()
    print("=" * 90)
    print(f"  Monthly Flow Strategy — {ticker}  ({START} → {END})")
    print(f"  Fee: {FEE_BPS}bps/trade  |  Capital: ${CAPITAL:,.0f}")
    print("=" * 90)
    print(f"  {'Config':<20} {'TotalRet':>9} {'AnnRet':>8} {'Sharpe':>7} "
          f"{'Calmar':>7} {'MaxDD':>7} {'Win%':>6} {'Trades':>7} {'+Mo':>5} {'-Mo':>5}")
    print("  " + "-" * 88)
    for _, row in df.drop(columns=["_res"]).iterrows():
        print(f"  {row['config']:<20} {row['total_return']:>8.1%} {row['ann_return']:>7.1%} "
              f"{row['sharpe']:>7.2f} {row['calmar']:>7.2f} {row['max_dd']:>7.1%} "
              f"{row['win_rate']:>5.1%} {row['trades']:>7} "
              f"{row['pos_months']:>5} {row['neg_months']:>5}")
    print("=" * 90)


def print_monthly(res: pd.DataFrame, label: str) -> None:
    monthly = res["equity"].resample("ME").last().pct_change().dropna()
    bm_m    = res["benchmark"].resample("ME").last().pct_change().dropna()
    print(f"\n  Monthly returns — {label}")
    print(f"  {'Month':<10} {'Strategy':>10} {'BTC Buy&Hold':>13} {'Edge':>8}")
    print("  " + "-" * 45)
    for dt in monthly.index[-24:]:   # last 24 months
        s = monthly.get(dt, 0)
        b = bm_m.get(dt, 0)
        print(f"  {dt.strftime('%Y-%m'):<10} {s:>+9.2%} {b:>+12.2%} {s-b:>+7.2%}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tickers = {
        "BTC-USD": "Bitcoin",
        "ETH-USD": "Ethereum",
        "TLT":     "TLT Bonds (reference)",
    }

    best_results = {}

    for ticker, name in tickers.items():
        print(f"\nFetching {name} ({ticker})...")
        try:
            close = fetch(ticker)
            print(f"  {len(close):,} daily bars  {close.index[0].date()} → {close.index[-1].date()}")
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        df = sweep(close, ticker)
        print_sweep(df, ticker)

        # print monthly breakdown for best config (by Sharpe)
        best_row = df.loc[df["sharpe"].idxmax()]
        best_res = best_row["_res"]
        best_results[ticker] = {"res": best_res, "config": best_row["config"], "m": best_row}
        print_monthly(best_res, f"{ticker} best config: {best_row['config']}")

    # Combined portfolio: BTC + ETH equal weight
    print("\n\n" + "=" * 60)
    print("  Combined BTC + ETH equal-weight monthly flow")
    print("=" * 60)
    if "BTC-USD" in best_results and "ETH-USD" in best_results:
        btc_net = best_results["BTC-USD"]["res"]["net"]
        eth_net = best_results["ETH-USD"]["res"]["net"]
        combined = (btc_net + eth_net) / 2
        combined = combined.reindex(btc_net.index).fillna(0)
        eq_comb = (1 + combined).cumprod() * CAPITAL
        btc_bm  = best_results["BTC-USD"]["res"]["benchmark"]
        daily_std = combined.std()
        sharpe_c = combined.mean() / daily_std * math.sqrt(252) if daily_std > 0 else 0
        dd_c = (eq_comb - eq_comb.cummax()) / eq_comb.cummax()
        print(f"  Total return    : {(eq_comb.iloc[-1]-CAPITAL)/CAPITAL:+.1%}")
        print(f"  Ann return      : {(eq_comb.iloc[-1]/CAPITAL)**(252/len(eq_comb))-1:+.1%}")
        print(f"  Sharpe          : {sharpe_c:.2f}")
        print(f"  Max DD          : {dd_c.min():.1%}")
        monthly_c = eq_comb.resample("ME").last().pct_change().dropna()
        print(f"  Positive months : {(monthly_c > 0).sum()} / {len(monthly_c)}")
        print()
        print("  Monthly P&L (last 24 months):")
        for dt in monthly_c.index[-24:]:
            v = monthly_c.get(dt, 0)
            bar = "+" * int(abs(v) * 100) if v > 0 else "-" * int(abs(v) * 100)
            print(f"    {dt.strftime('%Y-%m')}  {v:>+7.2%}  {bar[:30]}")
