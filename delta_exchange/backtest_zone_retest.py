"""Zone-touch + retest + break backtest — Pure SL/TP exits.

PATTERN (per-bar on 5m candles):
  • Compute supply/demand zones from prior 200 5m bars (look-back only, no peek)
  • LONG setup (green/demand zone):
      1. Some recent bar (within last LOOKBACK_BARS) dipped INTO the zone
         (bar low <= zone_top AND bar low >= zone_bottom)
      2. Price then CLOSED back above zone_top (rejection)
      3. Current bar CLOSES above the rejection bar's high (break confirmation)
      → Enter LONG at current close.
  • SHORT setup (red/supply zone): mirror image.

EXITS: Pure SL/TP at ±1.5% / +1.0% (matches v5.5 production regime).
Stop / target checked intra-bar on 1m high/low (matches Delta bracket order).
"""
from __future__ import annotations
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
import pandas as pd

from core.sr_levels import compute_sr_levels

# Dials (mirror v5.5 production exit-side for apples-to-apples)
USD_INR_RATE   = 86.0
START_INR      = 40_000.0
CAPITAL_USE_PCT = 0.50
LEVERAGE       = 3.0
SL_PCT         = 0.015
TP_PCT         = 0.010
SIZE_MULT      = 1.0        # zone setups don't have "pred strength"; use flat 1×
MAX_HOLD_HOURS = 72
PERP_FEE_BPS   = 5.0
SLIPPAGE_BPS   = 2.0
MAINT_MARGIN_PCT = 0.005
MAX_CONCURRENT = 2

# Zone-detection knobs
ZONE_LOOKBACK_BARS  = 200   # 5m bars feeding sr_levels
SETUP_LOOKBACK_BARS = 10    # how far back to look for touch + rejection chain
MIN_ZONE_GAP_BARS   = 1     # bars between touch and rejection (≥1 = real reject)


def load_5m_bars(subdir: str, perp_symbol: str) -> pd.DataFrame:
    """Aggregate 1m mark candles to 5m OHLC. Volume is mostly empty on mark
    bars — we synthesize 1.0 so sr_levels' volume-weighted code paths still
    run. Zone detection is mostly price-action driven."""
    p = Path(__file__).parent / "data" / subdir / "perp" / f"{perp_symbol}_mark_1m.csv"
    df = pd.read_csv(p)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    bars_5m = df.resample("5min").agg({"Open": "first", "High": "max",
                                        "Low": "min", "Close": "last"}).dropna()
    bars_5m["Volume"] = 1.0    # synthetic — sr_levels needs the column
    return bars_5m


def load_1m_hl(subdir: str, perp_symbol: str) -> pd.DataFrame:
    p = Path(__file__).parent / "data" / subdir / "perp" / f"{perp_symbol}_mark_1m.csv"
    df = pd.read_csv(p)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")[["high", "low", "close"]].sort_index()


def _detect_setup(bars: pd.DataFrame, zones: list, side: str) -> dict | None:
    """Look for touch + rejection + break in the last SETUP_LOOKBACK_BARS.
    side ∈ {'long', 'short'}. zones is a list of demand_zones (for long)
    or supply_zones (for short)."""
    recent = bars.iloc[-SETUP_LOOKBACK_BARS:]
    current = recent.iloc[-1]

    for zone in zones:
        ztop = float(zone["top"])
        zbot = float(zone["bottom"])
        if not (ztop > zbot): continue

        # Step 1 — touch in last N-2 bars (need room for rejection + break)
        touch_window = recent.iloc[:-2]
        if side == "long":
            touches = touch_window[(touch_window["Low"] <= ztop) &
                                    (touch_window["Low"] >= zbot)]
        else:
            touches = touch_window[(touch_window["High"] >= zbot) &
                                    (touch_window["High"] <= ztop)]
        if touches.empty: continue
        touch_t = touches.index[-1]

        # Step 2 — rejection: a bar after touch closes back away from zone
        after_touch = recent[recent.index > touch_t]
        if after_touch.empty: continue
        if side == "long":
            rejections = after_touch[after_touch["Close"] > ztop]
        else:
            rejections = after_touch[after_touch["Close"] < zbot]
        if rejections.empty: continue
        rej_bar = rejections.iloc[0]
        rej_t = rejections.index[0]

        # Step 3 — current bar breaks rejection bar's extreme
        # We need at least MIN_ZONE_GAP_BARS between touch and rejection
        # and CURRENT bar must be AFTER rejection (not equal).
        if rej_t == current.name: continue
        bars_between = (rej_t - touch_t).total_seconds() / 300
        if bars_between < MIN_ZONE_GAP_BARS: continue

        if side == "long":
            if current["Close"] > rej_bar["High"]:
                return {"side": "long", "zone": zone, "rej_high": float(rej_bar["High"]),
                        "rej_low": float(rej_bar["Low"]), "touch_t": touch_t,
                        "rej_t": rej_t}
        else:
            if current["Close"] < rej_bar["Low"]:
                return {"side": "short", "zone": zone, "rej_high": float(rej_bar["High"]),
                        "rej_low": float(rej_bar["Low"]), "touch_t": touch_t,
                        "rej_t": rej_t}
    return None


def run_zone_retest(asset: str, subdir: str, perp_sym: str, start_usd: float,
                    leverage: float = 3.0):
    bars_5m = load_5m_bars(subdir, perp_sym)
    hl_1m = load_1m_hl(subdir, perp_sym)

    equity = start_usd
    open_pos = None    # only one per asset at a time (simpler than multi)
    trades = []
    timeline = list(bars_5m.index)

    for i, t in enumerate(timeline):
        if i < ZONE_LOOKBACK_BARS + SETUP_LOOKBACK_BARS:
            continue

        # --- manage open position FIRST (intra-bar 1m check) ---
        if open_pos is not None:
            sign = 1 if open_pos["side"] == "long" else -1
            entry_px = open_pos["entry_px"]
            sl_price = entry_px * (1 - sign * SL_PCT)
            tp_price = entry_px * (1 + sign * TP_PCT)
            liq_pct = (1.0 / leverage) - MAINT_MARGIN_PCT
            liq_price = entry_px * (1 - sign * liq_pct)
            check_from = open_pos.get("last_check_t", open_pos["entry_t"])
            slice_1m = hl_1m[(hl_1m.index > check_from) & (hl_1m.index <= t)]
            max_adverse = open_pos.get("max_adverse_pct", 0.0)
            exit_t = exit_px = reason = None
            for mt, row in slice_1m.iterrows():
                adv = -sign * ((row["low" if sign == 1 else "high"]) - entry_px) / entry_px
                if adv > max_adverse: max_adverse = adv
                if sign == 1:
                    if row["low"] <= liq_price: exit_t, exit_px, reason = mt, liq_price, "LIQ"; break
                    if row["low"] <= sl_price:  exit_t, exit_px, reason = mt, sl_price, "stop"; break
                    if row["high"] >= tp_price: exit_t, exit_px, reason = mt, tp_price, "target"; break
                else:
                    if row["high"] >= liq_price: exit_t, exit_px, reason = mt, liq_price, "LIQ"; break
                    if row["high"] >= sl_price:  exit_t, exit_px, reason = mt, sl_price, "stop"; break
                    if row["low"] <= tp_price:   exit_t, exit_px, reason = mt, tp_price, "target"; break
            held_h = (t - open_pos["entry_t"]).total_seconds() / 3600
            if reason is None and held_h >= MAX_HOLD_HOURS:
                reason = "max_hold"; exit_t = t
                exit_px = float(hl_1m.loc[t]["close"]) if t in hl_1m.index else entry_px
            if reason:
                fill = exit_px * (1 - sign * SLIPPAGE_BPS / 1e4)
                raw = sign * (fill - entry_px) / entry_px
                net = raw - 2 * PERP_FEE_BPS / 1e4
                pnl = open_pos["notional"] * net
                equity += pnl
                trades.append({**open_pos, "exit_t": exit_t, "exit_px": fill,
                               "pnl_usd": pnl, "net_ret": net, "exit_reason": reason,
                               "max_adverse_pct": max_adverse, "liq_price": liq_price,
                               "sl_price": sl_price, "tp_price": tp_price,
                               "equity_after": equity})
                open_pos = None
            else:
                open_pos["last_check_t"] = t
                open_pos["max_adverse_pct"] = max_adverse

        # --- look for new setup ---
        if open_pos is not None: continue

        window = bars_5m.iloc[i - ZONE_LOOKBACK_BARS : i + 1]
        try:
            sr = compute_sr_levels(window, tolerance=window["Close"].mean() * 0.00087)
        except Exception:
            continue

        # Try LONG (demand) first, then SHORT (supply) — only one per bar
        setup = _detect_setup(window, sr.get("demand_zones", []), "long")
        if setup is None:
            setup = _detect_setup(window, sr.get("supply_zones", []), "short")
        if setup is None: continue

        side = setup["side"]
        side_sign = 1 if side == "long" else -1
        entry_px = float(bars_5m.iloc[i]["Close"]) * (1 + side_sign * SLIPPAGE_BPS / 1e4)
        eq_eff = equity * CAPITAL_USE_PCT
        desired = eq_eff * SIZE_MULT
        # Margin cap
        notional = min(desired, equity * leverage)
        if notional <= 0: continue
        open_pos = {
            "asset": asset, "side": side, "side_sign": side_sign,
            "entry_t": t, "entry_px": entry_px, "notional": notional,
            "leverage": leverage,
            "zone_top": float(setup["zone"]["top"]),
            "zone_bottom": float(setup["zone"]["bottom"]),
            "rej_high": setup["rej_high"], "rej_low": setup["rej_low"],
            "touch_t": setup["touch_t"], "rej_t": setup["rej_t"],
            "last_check_t": t, "max_adverse_pct": 0.0,
        }

    return {"asset": asset, "trades": trades, "equity_final": equity, "n_trades": len(trades)}


def summarize(label: str, r: dict, start_usd: float):
    trades = r["trades"]
    if not trades:
        print(f"  {label:<20} 0 trades"); return
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    wr = len(wins) / len(trades) * 100
    inr = total * USD_INR_RATE
    by_reason = {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    rs = ", ".join(f"{k}:{v}" for k, v in sorted(by_reason.items()))
    print(f"  {label:<20} n={len(trades):>3}  win%={wr:>5.1f}  "
          f"total=${total:>+7.2f} ({'+' if inr>=0 else ''}{inr:>+7,.0f} INR)  "
          f"avgW=${(sum(wins)/len(wins) if wins else 0):>+5.2f}  "
          f"avgL=${(sum(losses)/len(losses) if losses else 0):>+5.2f}  "
          f"reasons=[{rs}]")


def main():
    start_usd = START_INR / USD_INR_RATE
    print("=" * 110)
    print(f"  Zone-touch + retest + break backtest — Pure SL/TP exits")
    print(f"  ₹40k seed (${start_usd:.2f}), 3× leverage, BTC + ETH independent pools")
    print(f"  Setup: touch zone → close back across zone (reject) → next bar breaks reject extreme")
    print(f"  Exit:  ±1.5% SL or +1.0% TP, full position (no trail, no partial)")
    print("=" * 110)

    btc = run_zone_retest("BTC", "june_btc", "BTCUSD", start_usd)
    eth = run_zone_retest("ETH", "june_eth", "ETHUSD", start_usd)

    print("\n  ── FULL WINDOW (Jun 2 → Jun 10) per asset (independent ₹40k seeds) ──")
    summarize("BTC zone retest", btc, start_usd)
    summarize("ETH zone retest", eth, start_usd)

    combined_pnl = (btc["equity_final"] - start_usd) + (eth["equity_final"] - start_usd)
    print(f"\n  COMBINED (BTC + ETH, independent pools): ${combined_pnl:+.2f} "
          f"({'+' if combined_pnl>=0 else ''}{combined_pnl*USD_INR_RATE:,.0f} INR)")

    # Sample trades
    for asset_name, r in [("BTC", btc), ("ETH", eth)]:
        if not r["trades"]: continue
        print(f"\n  ── {asset_name} trade log (first 12) ──")
        for t in r["trades"][:12]:
            ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%m-%d %H:%M IST")
            xist = t["exit_t"].tz_convert("Asia/Kolkata").strftime("%m-%d %H:%M IST")
            held = (t["exit_t"] - t["entry_t"]).total_seconds() / 3600
            side = "LONG " if t["side"] == "long" else "SHORT"
            print(f"    {ist:<14} {side}  entry ${t['entry_px']:>10,.2f}  →  "
                  f"exit {xist:<14} ${t['exit_px']:>10,.2f}  "
                  f"net {t['net_ret']*100:>+6.3f}%  ({held:>5.1f}h)  "
                  f"reason={t['exit_reason']:<8}  PnL=${t['pnl_usd']:>+6.2f}")

    # vs v5.5 baseline for context
    print("\n" + "=" * 110)
    print("  CONTEXT — v5.5 production results on same window:")
    print(f"    v5.5 trail+partial (BTC+ETH combined):    +₹13,443  (42 trades, 90.5% WR)")
    print(f"    v5.5 pure_sltp     (BTC+ETH combined):    +₹14,407  (38 trades, 92.1% WR)")
    print("=" * 110)


if __name__ == "__main__":
    main()
