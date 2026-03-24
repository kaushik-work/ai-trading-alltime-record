import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import pandas as pd
from datetime import datetime
import config
from core.memory import init_db, TradeMemory
from core.records import init_records_db, RecordTracker
from core import ipc
from data.market import RealMarketData

st.set_page_config(page_title="AishQuant", page_icon="⚡", layout="wide")

st.markdown("""
<style>
.aq-logo {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 0 12px 0;
}
.aq-logo-icon {
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    color: #00d4ff; font-size: 26px; font-weight: 900;
    width: 44px; height: 44px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    border: 1.5px solid #00d4ff33;
}
.aq-logo-text { line-height: 1.1; }
.aq-logo-text .name { font-size: 18px; font-weight: 800; color: #f0f0f0; }
.aq-logo-text .tag  { font-size: 10px; color: #888; letter-spacing: 1.5px; text-transform: uppercase; }
.mode-switch-wrap {
    position: fixed; top: 14px; right: 60px; z-index: 9999;
    display: flex; align-items: center; gap: 8px;
    background: #1e1e2e; border: 1px solid #333;
    border-radius: 24px; padding: 5px 14px 5px 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
.mode-switch-label { font-size: 11px; font-weight: 700; letter-spacing: 1px; }
.mode-switch-label.paper { color: #4fc3f7; }
.mode-switch-label.live  { color: #ef5350; }
.switch-track {
    width: 36px; height: 20px; border-radius: 10px;
    display: flex; align-items: center; padding: 2px;
    transition: background 0.3s;
}
.switch-track.paper { background: #1a3a5c; justify-content: flex-start; }
.switch-track.live  { background: #5c1a1a; justify-content: flex-end; }
.switch-knob {
    width: 16px; height: 16px; border-radius: 50%;
}
.switch-knob.paper { background: #4fc3f7; }
.switch-knob.live  { background: #ef5350; }
</style>
""", unsafe_allow_html=True)

init_db()
init_records_db()
memory = TradeMemory()
records = RecordTracker()

# ── Broker connection helper ──────────────────────────────────────────────────

def _test_broker_connection() -> dict:
    """Attempt a real broker connection. Always runs fresh (not cached)."""
    if config.IS_PAPER:
        return {"status": "ok", "mode": "paper", "message": "Paper mode — no broker needed"}

    missing = [k for k, v in {
        "ZERODHA_USER_ID": config.ZERODHA_USER_ID,
        "ZERODHA_PASSWORD": config.ZERODHA_PASSWORD,
        "ZERODHA_TOTP_SECRET": config.ZERODHA_TOTP_SECRET,
    }.items() if not v]
    if missing:
        return {"status": "misconfigured", "missing": missing,
                "message": f"Missing: {', '.join(missing)}"}
    try:
        from jugaad_trader import Zerodha
        broker = Zerodha(
            user_id=config.ZERODHA_USER_ID,
            password=config.ZERODHA_PASSWORD,
            twofa=config.ZERODHA_TOTP_SECRET,
        )
        broker.login()
        margins = broker.margins()
        cash = margins.get("equity", {}).get("available", {}).get("cash", 0)
        return {"status": "ok", "mode": "live", "balance": cash, "message": "Connected"}
    except ImportError:
        return {"status": "error", "message": "jugaad-trader not installed. Run: pip install jugaad-trader"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@st.cache_data(ttl=60)
def check_broker_connection() -> dict:
    return _test_broker_connection()


@st.cache_data(ttl=300)
def fetch_watchlist_quotes(symbols: tuple) -> dict:
    """Fetch real NSE prices via yfinance. Cached 5 min."""
    md = RealMarketData()
    return {s: md.get_quote(s) for s in symbols}


# ── Sidebar ───────────────────────────────────────────────────────────────────

# Logo
st.sidebar.markdown("""
<div class="aq-logo">
  <div class="aq-logo-icon">⚡</div>
  <div class="aq-logo-text">
    <div class="name">AishQuant</div>
    <div class="tag">AI Trading System</div>
  </div>
</div>
""", unsafe_allow_html=True)

st.sidebar.markdown(f"🕐 `{datetime.now().strftime('%H:%M:%S IST')}`")
st.sidebar.divider()

# ── Broker Status ─────────────────────────────────────────────────────────────
conn = check_broker_connection()
if conn["status"] == "ok":
    if conn["mode"] == "live":
        st.sidebar.success(f"Broker: Connected | ₹{conn.get('balance', 0):,.0f}")
    else:
        st.sidebar.info("Broker: Paper mode")
elif conn["status"] == "misconfigured":
    st.sidebar.warning("Broker: Not configured")
else:
    st.sidebar.error("Broker error — see Setup tab")

st.sidebar.divider()

# ── Strategies Registry ───────────────────────────────────────────────────────
st.sidebar.subheader("Strategies")
STRATEGIES = [
    {"name": "AishDoc Intraday", "symbols": "NIFTY, BANKNIFTY", "tf": "5m / 15m", "status": "Active"},
    # Phase 7 — add swing strategies here when built
]
for s in STRATEGIES:
    with st.sidebar.expander(f"{'🟢' if s['status'] == 'Active' else '🔴'} {s['name']}"):
        st.markdown(f"**Symbols:** {s['symbols']}")
        st.markdown(f"**Timeframe:** {s['tf']}")
        st.markdown(f"**Status:** {s['status']}")

st.sidebar.divider()

# ── Bot Controls (bottom) ─────────────────────────────────────────────────────
st.sidebar.subheader("Bot Controls")

is_paused = ipc.flag_exists(ipc.FLAG_PAUSE)
if is_paused:
    st.sidebar.error("Status: PAUSED")
else:
    st.sidebar.success("Status: Running")

col1, col2, col3 = st.sidebar.columns(3)
if col1.button("⏸", help="Pause Bot", type="primary", disabled=is_paused):
    ipc.write_flag(ipc.FLAG_PAUSE)
    ipc.clear_flag(ipc.FLAG_RESUME)
    st.rerun()

if col2.button("▶", help="Resume Bot", disabled=not is_paused):
    ipc.write_flag(ipc.FLAG_RESUME)
    ipc.clear_flag(ipc.FLAG_PAUSE)
    st.rerun()

if col3.button("↻", help="Refresh Dashboard"):
    st.rerun()

st.sidebar.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_dashboard, tab_trade, tab_backtest, tab_setup = st.tabs(["Dashboard", "Manual Trade", "Backtest", "Setup & Configuration"])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — DASHBOARD
# ════════════════════════════════════════════════════════════════════════════════
with tab_dashboard:

    # ── KPI Row ───────────────────────────────────────────────────────────────
    all_trades = memory.get_all_trades(limit=500)
    today_trades = memory.get_today_trades()
    all_records = records.get_all_records()

    total_pnl = sum(t.get("pnl", 0) for t in all_trades)
    today_pnl = sum(t.get("pnl", 0) for t in today_trades)
    win_trades = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
    win_rate = (win_trades / len(all_trades) * 100) if all_trades else 0
    open_positions = len([t for t in all_trades if t.get("side") == "BUY" and not t.get("closed_at")])

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Trades", len(all_trades))
    k2.metric("Today's Trades", len(today_trades))
    k3.metric("Total P&L", f"₹{total_pnl:,.2f}", delta=f"₹{today_pnl:,.2f} today")
    k4.metric("Win Rate", f"{win_rate:.1f}%")
    k5.metric("Open Positions", open_positions)

    st.divider()

    # ── All-Time Records ───────────────────────────────────────────────────────
    st.subheader("All-Time Records")
    if all_records:
        rec_data = [
            {"Record": r["description"], "Value": r["value"],
             "Symbol": r.get("symbol") or "-", "Date": r["achieved_at"][:10]}
            for r in all_records.values()
        ]
        st.dataframe(pd.DataFrame(rec_data), use_container_width=True, hide_index=True)
    else:
        st.info("No records yet. Start trading!")

    st.divider()

    # ── Recent Trades + Today's Summary ───────────────────────────────────────
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("Recent Trades")
        if all_trades:
            df = pd.DataFrame(all_trades[:50])
            cols = ["symbol", "side", "quantity", "price", "pnl", "status", "confidence", "timestamp"]
            df = df[[c for c in cols if c in df.columns]]
            df["pnl"] = df["pnl"].map(lambda x: f"₹{x:,.2f}")
            df["confidence"] = df["confidence"].map(lambda x: f"{x*100:.0f}%" if x else "-")
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No trades yet.")

    with col_right:
        st.subheader("Today's Summary")
        if today_trades:
            wins = sum(1 for t in today_trades if t.get("pnl", 0) > 0)
            losses = sum(1 for t in today_trades if t.get("pnl", 0) < 0)
            st.metric("Trades Today", len(today_trades))
            st.metric("Wins / Losses", f"{wins} / {losses}")
            st.metric("Today P&L", f"₹{today_pnl:,.2f}")

            pnl_data = pd.DataFrame([
                {"Symbol": t["symbol"], "P&L": t.get("pnl", 0)} for t in today_trades
            ])
            if not pnl_data.empty:
                st.bar_chart(pnl_data.set_index("Symbol"))
        else:
            st.info("No trades today.")

    st.divider()

    # ── Watchlist with real prices ─────────────────────────────────────────────
    st.subheader("Watchlist — Live Prices")
    watchlist_flat = [s for symbols in config.WATCHLIST.values() for s in symbols]

    with st.spinner("Fetching prices..."):
        quotes = fetch_watchlist_quotes(tuple(watchlist_flat[:10]))

    wl_cols = st.columns(min(len(watchlist_flat), 5))
    for i, symbol in enumerate(watchlist_flat[:10]):
        q = quotes.get(symbol, {})
        price = q.get("last_price", 0)
        change_pct = q.get("change_pct", 0)
        source = q.get("source", "")
        label = f"₹{price:,.2f}" if price else "N/A"
        delta = f"{change_pct:+.2f}%" if price else ""
        help_txt = "mock fallback — yfinance unavailable" if source == "mock_fallback" else "via yfinance"
        wl_cols[i % 5].metric(symbol, label, delta, help=help_txt)

    st.divider()

    # ── P&L Equity Curve ──────────────────────────────────────────────────────
    st.subheader("P&L Equity Curve")
    sell_trades = [t for t in all_trades if t.get("side") == "SELL" and t.get("pnl") is not None]
    if sell_trades:
        df_pnl = pd.DataFrame(sell_trades)[["timestamp", "pnl", "symbol"]].copy()
        df_pnl["timestamp"] = pd.to_datetime(df_pnl["timestamp"])
        df_pnl = df_pnl.sort_values("timestamp")
        df_pnl["cumulative_pnl"] = df_pnl["pnl"].cumsum()
        df_pnl["date"] = df_pnl["timestamp"].dt.date

        col_curve, col_daily = st.columns([3, 1])
        with col_curve:
            st.line_chart(df_pnl.set_index("timestamp")["cumulative_pnl"],
                          use_container_width=True)
            st.caption("Cumulative P&L over all closed trades")

        with col_daily:
            daily_pnl = df_pnl.groupby("date")["pnl"].sum().reset_index()
            daily_pnl.columns = ["Date", "Daily P&L (₹)"]
            daily_pnl["Daily P&L (₹)"] = daily_pnl["Daily P&L (₹)"].round(2)
            st.dataframe(daily_pnl.sort_values("Date", ascending=False),
                         use_container_width=True, hide_index=True)
    else:
        st.info("No closed trades yet — equity curve will appear after your first SELL.")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — MANUAL TRADE
# ════════════════════════════════════════════════════════════════════════════════
with tab_trade:
    st.title("Manual Trade Override")
    st.warning("This sends a trade directly to the bot at the next 5-minute cycle. Use carefully.")

    watchlist_all = [s for syms in config.WATCHLIST.values() for s in syms]

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        symbol = st.selectbox("Symbol", watchlist_all)
    with col_b:
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True)
    with col_c:
        quantity = st.number_input("Quantity", min_value=1, max_value=1000, value=1, step=1)

    reason = st.text_input("Reason (for the trade log)", value="Manual override from dashboard")

    # Show current price for reference
    if symbol:
        with st.spinner(f"Fetching {symbol} price..."):
            q = fetch_watchlist_quotes((symbol,)).get(symbol, {})
        price = q.get("last_price", 0)
        if price:
            est_value = price * quantity
            st.info(f"Current price: ₹{price:,.2f} | Estimated order value: ₹{est_value:,.2f}")

    st.divider()

    # Confirmation gate — user must tick a checkbox before the button is enabled
    confirm = st.checkbox(f"I confirm: {side} {quantity} × {symbol} at market price")

    if st.button("Send Trade to Bot", type="primary", disabled=not confirm):
        if ipc.flag_exists(ipc.FLAG_FORCE_TRADE):
            st.error("A force trade is already queued and waiting. Wait for the bot to process it first.")
        else:
            ipc.write_force_trade(symbol, side, int(quantity), reason)
            st.success(
                f"Trade queued: **{side} {quantity} × {symbol}**\n\n"
                f"The bot will execute it at the next 5-minute cycle."
            )

    # Show if a force trade is currently pending
    if ipc.flag_exists(ipc.FLAG_FORCE_TRADE):
        st.info("A manual trade is currently queued and waiting for the bot to pick it up.")

    st.divider()
    st.subheader("Open Positions")
    open_buys = [t for t in all_trades if t.get("side") == "BUY" and not t.get("closed_at") and t.get("status") in ("COMPLETE", "PLACED")]
    if open_buys:
        df_open = pd.DataFrame(open_buys)[["symbol", "quantity", "price", "timestamp", "confidence"]]
        df_open.columns = ["Symbol", "Qty", "Avg Buy Price", "Opened At", "AI Confidence"]
        # Enrich with current prices
        open_symbols = tuple(df_open["Symbol"].unique())
        live_quotes = fetch_watchlist_quotes(open_symbols)
        df_open["Current Price"] = df_open["Symbol"].map(
            lambda s: live_quotes.get(s, {}).get("last_price", 0)
        )
        df_open["Unrealized P&L %"] = (
            (df_open["Current Price"] - df_open["Avg Buy Price"]) / df_open["Avg Buy Price"] * 100
        ).round(2)
        st.dataframe(df_open, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions.")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — BACKTEST
# ════════════════════════════════════════════════════════════════════════════════
with tab_backtest:
    st.title("Strategy Backtester — AishDoc Intraday")
    st.caption("Replays 15-min candles through the signal scorer. Uses yfinance (max 60 days).")

    # ── Controls ──────────────────────────────────────────────────────────────
    bc1, bc2, bc3, bc4 = st.columns(4)
    with bc1:
        bt_symbol = st.selectbox("Symbol", ["NIFTY", "BANKNIFTY"], key="bt_sym")
    with bc2:
        bt_interval = st.selectbox("Timeframe", ["5m", "15m"], index=1, key="bt_interval")
    with bc3:
        bt_period = st.selectbox("Period", ["30d", "45d", "60d"], index=2, key="bt_period")
    with bc4:
        bt_capital = st.number_input("Starting Capital (₹)", min_value=5000,
                                     max_value=500000, value=20000, step=5000,
                                     key="bt_capital")

    bc4, bc5, bc6 = st.columns(3)
    with bc4:
        bt_min_score = st.slider("Min Signal Score", min_value=5, max_value=9,
                                 value=7, step=1, key="bt_score",
                                 help="Higher = fewer but higher-conviction trades")
    with bc5:
        bt_risk_pct = st.slider("Risk per Trade (%)", min_value=1.0, max_value=5.0,
                                value=2.0, step=0.5, key="bt_risk",
                                help="% of equity risked per trade")
    with bc6:
        bt_daily_loss = st.slider("Daily Loss Limit (%)", min_value=1.0, max_value=5.0,
                                  value=3.0, step=0.5, key="bt_dloss",
                                  help="Stop trading for the day after losing this %")

    run_bt = st.button("Run Backtest", type="primary")

    if run_bt:
        with st.spinner(f"Fetching {bt_period} of 15-min data for {bt_symbol}…"):
            try:
                from backtesting.engine import BacktestEngine
                from backtesting.metrics import compute_metrics

                engine = BacktestEngine(initial_capital=float(bt_capital))
                result = engine.run(bt_symbol, period=bt_period,
                                    interval=bt_interval,
                                    min_score=bt_min_score,
                                    risk_pct=bt_risk_pct,
                                    daily_loss_limit_pct=bt_daily_loss)
                metrics = compute_metrics(result["trades"], result["equity_curve"],
                                          result["initial_capital"])
                st.session_state["bt_result"]  = result
                st.session_state["bt_metrics"] = metrics
                st.success(f"Backtest complete — {len(result['trades'])} trades simulated.")
            except Exception as e:
                st.error(f"Backtest failed: {e}")

    # ── Results ───────────────────────────────────────────────────────────────
    if "bt_metrics" in st.session_state and "bt_result" in st.session_state:
        m  = st.session_state["bt_metrics"]
        r  = st.session_state["bt_result"]

        if m.get("error"):
            st.warning(m["error"])
        else:
            st.divider()
            st.subheader("Performance Summary")

            # KPI row 1
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Total Trades",   m["total_trades"])
            k2.metric("Win Rate",       f"{m['win_rate']}%")
            k3.metric("Total P&L",      f"₹{m['total_pnl']:,.2f}")
            k4.metric("Total Return",   f"{m['total_return_pct']}%")
            k5.metric("Max Drawdown",   f"{m['max_drawdown_pct']}%")

            # KPI row 2
            k6, k7, k8, k9, k10 = st.columns(5)
            k6.metric("Avg Win",        f"₹{m['avg_win']:,.2f}")
            k7.metric("Avg Loss",       f"₹{m['avg_loss']:,.2f}")
            k8.metric("R:R Ratio",      f"{m['rr_ratio']}x")
            k9.metric("Profit Factor",  m["profit_factor"])
            k10.metric("Sharpe Ratio",  m["sharpe_ratio"])

            st.divider()

            col_left, col_right = st.columns([3, 1])

            with col_left:
                # Equity curve
                st.subheader("Equity Curve")
                if r["equity_curve"]:
                    df_eq = pd.DataFrame(r["equity_curve"])
                    df_eq["date"] = pd.to_datetime(df_eq["date"])
                    st.line_chart(df_eq.set_index("date")["equity"],
                                  use_container_width=True)
                    st.caption(f"Starting: ₹{r['initial_capital']:,.0f}  →  "
                               f"Final: ₹{r['final_equity']:,.0f}")

            with col_right:
                # Exit breakdown
                st.subheader("Exit Types")
                eb = m.get("exit_breakdown", {})
                if eb:
                    df_eb = pd.DataFrame(
                        [{"Exit": k, "Count": v} for k, v in eb.items()]
                    )
                    st.dataframe(df_eb, use_container_width=True, hide_index=True)

                st.subheader("Streak Stats")
                st.metric("Best Win Streak",  m["max_win_streak"])
                st.metric("Worst Loss Streak", m["max_loss_streak"])
                st.metric("Best Trade",  f"₹{m['best_trade']:,.2f}")
                st.metric("Worst Trade", f"₹{m['worst_trade']:,.2f}")

            st.divider()

            # Trade log
            st.subheader("Trade Log")
            if r["trades"]:
                df_t = pd.DataFrame(r["trades"])
                show_cols = [c for c in
                             ["symbol", "side", "entry_time", "entry_price",
                              "exit_time", "exit_price", "exit_reason",
                              "quantity", "atr", "score", "pnl", "equity"]
                             if c in df_t.columns]
                df_t = df_t[show_cols].copy()
                df_t["pnl"]    = df_t["pnl"].map(lambda x: f"₹{x:,.2f}")
                df_t["equity"] = df_t["equity"].map(lambda x: f"₹{x:,.2f}")
                st.dataframe(df_t, use_container_width=True, hide_index=True)
                st.caption(f"{len(r['trades'])} trades | "
                           f"Wins: {m['win_trades']} | Losses: {m['loss_trades']}")
            else:
                st.info("No trades fired — signal threshold may be too high for this period.")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — SETUP & CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════
with tab_setup:
    st.title("Setup & Configuration")

    # ── Trading Mode ─────────────────────────────────────────────────────────
    st.subheader("Trading Mode")
    if config.IS_PAPER:
        st.info("Current mode: **PAPER** (Simulation) — no real money at risk.")
    else:
        st.error("Current mode: **LIVE TRADING** — real money is being traded.")

    st.markdown("""
Edit `.env` to change the mode, then restart `main.py` and this dashboard:
```
TRADING_MODE=paper   # safe default — simulation only
TRADING_MODE=live    # real trades — ensure credentials are set first
```
""")

    st.divider()

    # ── Credentials Status ────────────────────────────────────────────────────
    st.subheader("Credentials Status")
    creds = {
        "ZERODHA_USER_ID": config.ZERODHA_USER_ID,
        "ZERODHA_PASSWORD": config.ZERODHA_PASSWORD,
        "ZERODHA_TOTP_SECRET": config.ZERODHA_TOTP_SECRET,
        "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
    }
    for key, val in creds.items():
        c1, c2 = st.columns([3, 1])
        c1.code(key)
        if val:
            c2.success("Set")
        else:
            c2.error("MISSING")

    st.divider()

    # ── Connection Test ───────────────────────────────────────────────────────
    st.subheader("Test Broker Connection")
    if st.button("Test Zerodha Connection", type="primary"):
        with st.spinner("Attempting login..."):
            result = _test_broker_connection()
        if result["status"] == "ok" and result["mode"] == "live":
            st.success(f"Connected! Available cash: ₹{result.get('balance', 0):,.2f}")
        elif result["status"] == "ok":
            st.info("Paper mode — no broker connection needed.")
        elif result["status"] == "misconfigured":
            st.warning(f"Credentials missing: {', '.join(result.get('missing', []))}")
        else:
            st.error(f"Connection failed: {result['message']}")
            st.caption("If TOTP failed, wait 30 seconds and try again — TOTP codes rotate every 30s.")

    st.divider()

    # ── Risk Parameters ───────────────────────────────────────────────────────
    st.subheader("Risk Parameters (current)")
    risk_df = pd.DataFrame([
        {"Parameter": "MAX_TRADE_AMOUNT", "Value": f"₹{config.MAX_TRADE_AMOUNT:,}", "Description": "Max spend per trade"},
        {"Parameter": "MAX_DAILY_LOSS", "Value": f"₹{config.MAX_DAILY_LOSS:,}", "Description": "Auto-pause bot if daily loss exceeds this"},
        {"Parameter": "MAX_OPEN_POSITIONS", "Value": str(config.MAX_OPEN_POSITIONS), "Description": "Max concurrent open positions"},
        {"Parameter": "STOP_LOSS_PCT", "Value": f"{config.STOP_LOSS_PCT}%", "Description": "Hard stop-loss per position (auto-sell)"},
        {"Parameter": "TAKE_PROFIT_PCT", "Value": f"{config.TAKE_PROFIT_PCT}%", "Description": "Trigger Claude review for exit"},
    ])
    st.dataframe(risk_df, use_container_width=True, hide_index=True)
    st.caption("To change these, edit `.env` and restart the bot.")

    st.divider()

    # ── Setup Guide ───────────────────────────────────────────────────────────
    st.subheader("Step-by-step: Going Live")
    st.markdown(f"""
### Step 1 — Get your Zerodha credentials
1. Log in to [kite.zerodha.com](https://kite.zerodha.com)
2. Your **User ID** is shown top-right (format: `AB1234`)
3. Your **Password** is your Zerodha login password

### Step 2 — Set up TOTP (2-Factor Auth)
1. Go to **My Account → Security** on the Zerodha web portal
2. Enable **Two-Factor Authentication**
3. When the QR code appears, click **"Can't scan? Get the key instead"**
4. Copy the 32-character base32 secret — this is your `ZERODHA_TOTP_SECRET`

### Step 3 — Fill your `.env` file
```
ZERODHA_USER_ID=AB1234
ZERODHA_PASSWORD=your_login_password
ZERODHA_TOTP_SECRET=JBSWY3DPEHPK3PXP...
TRADING_MODE=paper          # keep as paper until connection is verified
```

### Step 4 — Test the connection
Click **"Test Zerodha Connection"** above. If it shows your cash balance, you're good.

### Step 5 — Switch to live (when ready)
```
TRADING_MODE=live
```
Restart `main.py` and this dashboard. **Real money will be traded.**

> **Tip:** Run paper mode for at least a few days first to see how the bot behaves.
""")
