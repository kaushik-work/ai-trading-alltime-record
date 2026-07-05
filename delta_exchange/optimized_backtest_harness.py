"""
Optimized backtest harness for rapid strategy experimentation.
Loads BTC/ETH data ONCE, then runs many configurations quickly.

Uses the same core logic as backtest_engine.py but with:
- configurable sampling frequency (1m, 5m, 15m, 1h)
- configurable TTE window
- optional funding-rate filter
- optional composite-expiry signal
- optional spot-reference override
"""
from __future__ import annotations

import sys
import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

# ── defaults ─────────────────────────────────────────────────────────────────
USD_INR_RATE = 86.0
CAPITAL_USE_PCT = 0.50
MAX_CONCURRENT = 2
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
MAINT_MARGIN_PCT = 0.005


@dataclass
class Config:
    name: str
    freq_minutes: int = 60          # decision cadence: 1, 5, 15, 60
    tt_min_hours: float = 6.0
    tt_max_hours: float = 72.0
    entry_pct: float = 0.006
    persist_hours: int = 1
    moneyness: float = 0.05
    min_strikes: int = 3
    leverage: float = 3.0
    sl_pct: float = 0.015
    tp_pct: float = 0.010
    trail_peak_pct: float = 0.005
    trail_giveback: float = 0.0025
    max_hold_hours: float = 72.0
    size_base_pct: float = 0.005
    size_min_mult: float = 0.5
    size_max_mult: float = 3.0
    capital_use_pct: float = 0.50
    regime: str = "pure_sltp"       # 'pure_sltp' or 'trail_partial'
    # filters
    funding_filter: Optional[str] = None  # 'fade_longs', 'fade_shorts', None
    funding_threshold: float = 0.0        # annualized funding rate threshold
    composite_signal: bool = False        # TTE-weighted average across expiries
    spot_source: str = "perp"             # 'perp' or 'index'


class Engine:
    def __init__(self, data_subdir_btc: str, data_subdir_eth: str):
        self.btc_perp_raw, self.btc_marks_raw, self.btc_cat = self._load_data(data_subdir_btc, "BTCUSD")
        self.eth_perp_raw, self.eth_marks_raw, self.eth_cat = self._load_data(data_subdir_eth, "ETHUSD")
        self.btc_hl_raw = self._load_hl(data_subdir_btc, "BTCUSD")
        self.eth_hl_raw = self._load_hl(data_subdir_eth, "ETHUSD")
        # Try to load index / funding
        self.btc_index = self._load_index(data_subdir_btc, "BTCUSD")
        self.eth_index = self._load_index(data_subdir_eth, "ETHUSD")
        self.btc_funding = self._load_funding(data_subdir_btc, "BTCUSD")
        self.eth_funding = self._load_funding(data_subdir_eth, "ETHUSD")

    @staticmethod
    def _parse_symbol(sym: str):
        parts = sym.split("-")
        side, strike = parts[0], int(parts[2])
        dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
        return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")

    def _load_data(self, subdir: str, perp_symbol: str):
        base = Path(__file__).parent / "data" / subdir
        perp = pd.read_csv(base / "perp" / f"{perp_symbol}_mark_1m.csv")
        perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
        perp = perp.set_index("timestamp").sort_index()
        marks, rows = {}, []
        for p in sorted((base / "options").glob("*_mark_1m.csv")):
            sym = p.name.replace("_mark_1m.csv", "")
            df = pd.read_csv(p)
            if df.empty: continue
            df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
            marks[sym] = df.set_index("timestamp")["close"].sort_index()
            try:
                side, strike, exp = self._parse_symbol(sym)
                rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": exp})
            except Exception: pass
        return perp, marks, pd.DataFrame(rows)

    def _load_hl(self, subdir: str, perp_symbol: str) -> pd.DataFrame:
        p = Path(__file__).parent / "data" / subdir / "perp" / f"{perp_symbol}_mark_1m.csv"
        df = pd.read_csv(p)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("timestamp")[["high", "low", "close"]].sort_index()

    def _load_index(self, subdir: str, perp_symbol: str) -> Optional[pd.Series]:
        base = Path(__file__).parent / "data" / subdir / "index"
        candidates = [f"DEX{perp_symbol[:3]}USD_1m.csv", f"DEX{perp_symbol.replace('USD','')}USD_1m.csv"]
        for cand in candidates:
            p = base / cand
            if p.exists():
                df = pd.read_csv(p)
                df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
                return df.set_index("timestamp")["close"].sort_index()
        return None

    def _load_funding(self, subdir: str, perp_symbol: str) -> Optional[pd.Series]:
        p = Path(__file__).parent / "data" / subdir / "perp" / f"{perp_symbol}_funding_1h.csv"
        if not p.exists():
            return None
        df = pd.read_csv(p)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("timestamp")["funding_rate"].sort_index()

    def _resample(self, cfg: Config):
        """Resample raw 1m data to cfg.freq_minutes."""
        freq = f"{cfg.freq_minutes}min"
        # Perp close at decision times
        btc_perp = self.btc_perp_raw["close"].resample(freq).last().dropna()
        eth_perp = self.eth_perp_raw["close"].resample(freq).last().dropna()
        # H/L for exits
        btc_hl = self.btc_hl_raw.resample(freq).agg({"high": "max", "low": "min", "close": "last"}).dropna()
        eth_hl = self.eth_hl_raw.resample(freq).agg({"high": "max", "low": "min", "close": "last"}).dropna()
        # Option marks
        btc_marks = {sym: s.resample(freq).last().dropna() for sym, s in self.btc_marks_raw.items()}
        eth_marks = {sym: s.resample(freq).last().dropna() for sym, s in self.eth_marks_raw.items()}
        # Index
        btc_index = self.btc_index.resample(freq).last().dropna() if self.btc_index is not None else None
        eth_index = self.eth_index.resample(freq).last().dropna() if self.eth_index is not None else None
        return (btc_perp, btc_marks, btc_hl, btc_index,
                eth_perp, eth_marks, eth_hl, eth_index)

    def compute_pred(self, t, spot, catalogue, marks, cfg: Config):
        tt_min = t + pd.Timedelta(hours=cfg.tt_min_hours)
        tt_max = t + pd.Timedelta(hours=cfg.tt_max_hours)
        eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
        out = []
        for exp in sorted(eligible["expiry"].unique()):
            same = eligible[eligible["expiry"] == exp]
            calls = same[same["side"] == "C"].set_index("strike")
            puts  = same[same["side"] == "P"].set_index("strike")
            common = sorted(set(calls.index) & set(puts.index))
            near = [K for K in common if abs(K - spot) / spot <= cfg.moneyness]
            if len(near) < cfg.min_strikes: continue
            devs = []
            for K in near:
                c = marks.get(calls.loc[K, "symbol"])
                p = marks.get(puts.loc[K, "symbol"])
                if c is None or p is None: continue
                if t not in c.index or t not in p.index: continue
                cp, pp = float(c.loc[t]), float(p.loc[t])
                if cp <= 0 or pp <= 0: continue
                devs.append(((cp - pp + K) - spot) / spot)
            if len(devs) < cfg.min_strikes: continue
            pos = sum(1 for d in devs if d > 0)
            neg = sum(1 for d in devs if d < 0)
            if pos < cfg.min_strikes and neg < cfg.min_strikes: continue
            out.append({"expiry": exp, "pred": float(np.median(devs)),
                        "n_strikes": len(devs), "tte_h": (exp - t).total_seconds() / 3600})
        return out

    def run(self, cfg: Config, start_usd: float = 40000/86) -> dict:
        (btc_perp, btc_marks, btc_hl, btc_index,
         eth_perp, eth_marks, eth_hl, eth_index) = self._resample(cfg)

        equity = [start_usd]
        sig_hist = {"BTC": {}, "ETH": {}}
        open_pos = []
        trades = []

        hours = sorted(set(btc_perp.index) | set(eth_perp.index))

        for t in hours:
            # Build signal history
            for ak, perp, cat, marks, idx in [
                ("BTC", btc_perp, self.btc_cat, btc_marks, btc_index),
                ("ETH", eth_perp, self.eth_cat, eth_marks, eth_index),
            ]:
                if t not in perp.index: continue
                spot = float(perp.loc[t])
                if cfg.spot_source == "index" and idx is not None and t in idx.index:
                    spot = float(idx.loc[t])
                for p in self.compute_pred(t, spot, cat, marks, cfg):
                    sig_hist[ak].setdefault(p["expiry"], []).append((t, p["pred"]))

            # Trim history
            for ak in ("BTC", "ETH"):
                for exp in list(sig_hist[ak].keys()):
                    sig_hist[ak][exp] = [(ti, pi) for ti, pi in sig_hist[ak][exp]
                                         if (t - ti).total_seconds() <= 6 * 3600]

            # Manage open positions
            still_open = []
            for pos in open_pos:
                ak = pos["asset"]
                perp = btc_perp if ak == "BTC" else eth_perp
                hl = btc_hl if ak == "BTC" else eth_hl
                if t not in perp.index:
                    still_open.append(pos); continue
                close_px = float(perp.loc[t])
                if cfg.regime == "trail_partial":
                    events, closed = self._check_exit_trail_partial(pos, t, close_px, cfg, equity)
                else:
                    check_from = pos.get("last_check_t", pos["entry_t"])
                    hl_slice = hl[(hl.index > check_from) & (hl.index <= t)]
                    events, closed = self._check_exit_pure_sltp(pos, t, hl_slice, close_px, cfg, equity)
                    pos["last_check_t"] = t
                for ev in events:
                    trades.append({**pos, **ev})
                if not closed:
                    still_open.append(pos)
            open_pos = still_open

            # Entries
            if len(open_pos) >= MAX_CONCURRENT: continue
            for ak, perp, cat, marks, idx in [
                ("BTC", btc_perp, self.btc_cat, btc_marks, btc_index),
                ("ETH", eth_perp, self.eth_cat, eth_marks, eth_index),
            ]:
                if len(open_pos) >= MAX_CONCURRENT: break
                if ak in [p["asset"] for p in open_pos]: continue
                if t not in perp.index: continue
                spot = float(perp.loc[t])
                if cfg.spot_source == "index" and idx is not None and t in idx.index:
                    spot = float(idx.loc[t])
                preds = self.compute_pred(t, spot, cat, marks, cfg)
                if not preds: continue

                # Composite signal option
                candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
                if cfg.composite_signal:
                    weights = np.array([1.0 / max(p["tte_h"], 1.0) for p in preds])
                    weighted_pred = float(np.sum(np.array([p["pred"] for p in preds]) * weights) / weights.sum())
                    target = max(preds, key=lambda p: abs(p["pred"]))
                    candidates = [{**target, "pred": weighted_pred}]

                chosen = None
                already = {p["expiry"] for p in open_pos}
                for c in candidates:
                    if c["expiry"] in already: continue
                    if abs(c["pred"]) < cfg.entry_pct:
                        if not cfg.composite_signal:
                            break
                        continue
                    hist = sig_hist[ak].get(c["expiry"], [])
                    recent = [pi for ti, pi in hist
                              if (t - ti).total_seconds() <= cfg.persist_hours * 3600]
                    if len(recent) < cfg.persist_hours: continue
                    if sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"])) < cfg.persist_hours:
                        continue

                    # Funding filter
                    if cfg.funding_filter:
                        funding_series = self.btc_funding if ak == "BTC" else self.eth_funding
                        if funding_series is not None:
                            f = float(funding_series.asof(t)) if t >= funding_series.index.min() else 0.0
                            if cfg.funding_filter == "fade_longs" and f < cfg.funding_threshold:
                                continue
                            if cfg.funding_filter == "fade_shorts" and f > -cfg.funding_threshold:
                                continue

                    chosen = c; break
                if chosen is None: continue

                pred = chosen["pred"]
                side = 1 if pred > 0 else -1
                eq_eff = equity[0] * cfg.capital_use_pct
                sm = min(cfg.size_max_mult, max(cfg.size_min_mult, abs(pred) / cfg.size_base_pct))
                desired = eq_eff * sm
                margin_used = sum(p["notional"] / cfg.leverage for p in open_pos)
                margin_avail = max(0.0, equity[0] - margin_used)
                notional = min(desired, margin_avail * cfg.leverage)
                if notional <= 0: continue
                fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
                open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill, "side": side,
                                 "expiry": chosen["expiry"], "notional": notional,
                                 "size_mult": sm, "pred": pred, "peak": 0.0,
                                 "leverage": cfg.leverage,
                                 "last_check_t": t if cfg.regime == "pure_sltp" else None})

        return {"cfg": cfg, "trades": trades, "equity_final": equity[0]}

    def _check_exit_trail_partial(self, pos, t, close_px, cfg, equity):
        side = pos["side"]; entry_px = pos["entry_px"]
        unreal = side * (close_px - entry_px) / entry_px
        pos["peak"] = max(pos.get("peak", 0.0), unreal)
        events = []
        if (not pos.get("tp_taken")) and unreal >= cfg.tp_pct:
            half = pos["notional"] * 0.5
            fill = close_px * (1 - side * SLIPPAGE_BPS / 1e4)
            raw = side * (fill - entry_px) / entry_px
            net = raw - 2 * PERP_FEE_BPS / 1e4
            pnl = half * net
            equity[0] += pnl
            pos["notional"] -= half; pos["tp_taken"] = True
            events.append({"event": "partial_tp", "exit_t": t, "exit_px": fill,
                           "pnl_usd": pnl, "net_ret": net, "equity_after": equity[0]})
        held_h = (t - pos["entry_t"]).total_seconds() / 3600
        reason = None
        if t >= pos["expiry"]: reason = "expiry"
        elif held_h >= cfg.max_hold_hours: reason = "max_hold"
        elif unreal < -cfg.sl_pct: reason = "stop"
        elif pos["peak"] >= cfg.trail_peak_pct and (pos["peak"] - unreal) > cfg.trail_giveback:
            reason = "trail"
        if reason:
            fill = close_px * (1 - side * SLIPPAGE_BPS / 1e4)
            raw = side * (fill - entry_px) / entry_px
            net = raw - 2 * PERP_FEE_BPS / 1e4
            pnl = pos["notional"] * net
            equity[0] += pnl
            events.append({"event": reason, "exit_t": t, "exit_px": fill,
                           "pnl_usd": pnl, "net_ret": net, "equity_after": equity[0]})
            return events, True
        return events, False

    def _check_exit_pure_sltp(self, pos, t, hl_slice, close_px, cfg, equity):
        side = pos["side"]; entry_px = pos["entry_px"]
        sl_price = entry_px * (1 - side * cfg.sl_pct)
        tp_price = entry_px * (1 + side * cfg.tp_pct)
        liq_pct = (1.0 / cfg.leverage) - MAINT_MARGIN_PCT
        liq_price = entry_px * (1 - side * liq_pct)
        max_adverse = pos.get("max_adverse_pct", 0.0)
        exit_t = None; exit_px = None; reason = None
        for mt, row in hl_slice.iterrows():
            adv = -side * ((row["low" if side == 1 else "high"]) - entry_px) / entry_px
            if adv > max_adverse: max_adverse = adv
            if side == 1:
                if row["low"] <= liq_price: exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
                if row["low"] <= sl_price: exit_t = mt; exit_px = sl_price; reason = "stop"; break
                if row["high"] >= tp_price: exit_t = mt; exit_px = tp_price; reason = "target"; break
            else:
                if row["high"] >= liq_price: exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
                if row["high"] >= sl_price: exit_t = mt; exit_px = sl_price; reason = "stop"; break
                if row["low"] <= tp_price: exit_t = mt; exit_px = tp_price; reason = "target"; break
        pos["max_adverse_pct"] = max_adverse
        if reason is None:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            if t >= pos["expiry"]: reason = "expiry"; exit_t = t; exit_px = close_px
            elif held_h >= cfg.max_hold_hours: reason = "max_hold"; exit_t = t; exit_px = close_px
        if reason is None:
            return [], False
        fill = exit_px * (1 - side * SLIPPAGE_BPS / 1e4)
        raw = side * (fill - entry_px) / entry_px
        net = raw - 2 * PERP_FEE_BPS / 1e4
        pnl = pos["notional"] * net
        equity[0] += pnl
        return [{"event": reason, "exit_t": exit_t, "exit_px": fill,
                 "pnl_usd": pnl, "net_ret": net, "equity_after": equity[0],
                 "sl_price": sl_price, "tp_price": tp_price, "liq_price": liq_price,
                 "max_adverse_pct": max_adverse}], True


def summarize(result: dict) -> dict:
    trades = result["trades"]
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "final": result["equity_final"], "pf": 0.0}
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    total = sum(pnls)
    pf = sum(wins) / abs(sum(p for p in pnls if p <= 0)) if any(p <= 0 for p in pnls) else float("inf")
    return {"n": n, "wr": len(wins)/n*100, "pnl": total, "final": result["equity_final"], "pf": pf}


def fmt(cfg: Config, res: dict) -> str:
    s = summarize(res)
    return (f"{cfg.name:<32} {cfg.regime:<12} n={s['n']:>3}  WR={s['wr']:>5.1f}%  "
            f"P&L=${s['pnl']:>+7.2f}  final=${s['final']:>7.2f}  PF={s['pf']:>5.2f}")


if __name__ == "__main__":
    engine = Engine("june_daily_btc_focused", "june_daily_eth_focused")
    start_usd = 40000 / USD_INR_RATE

    configs = [
        Config("baseline_1h", freq_minutes=60, entry_pct=0.006, persist_hours=1),
        Config("5min", freq_minutes=5, entry_pct=0.006, persist_hours=1),
        Config("15min", freq_minutes=15, entry_pct=0.006, persist_hours=1),
        Config("1min", freq_minutes=1, entry_pct=0.006, persist_hours=1),
        Config("5min_low_gate", freq_minutes=5, entry_pct=0.0005, persist_hours=1),
        Config("15min_low_gate", freq_minutes=15, entry_pct=0.0005, persist_hours=1),
    ]

    print("\nGranularity test on clean daily-option data:\n")
    for cfg in configs:
        res = engine.run(cfg, start_usd)
        print(fmt(cfg, res))
