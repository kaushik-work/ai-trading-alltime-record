"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useWebSocket } from "./hooks/useWebSocket";
import Header from "./components/Header";

const _API    = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL  = _API.replace(/^http/, "ws") + "/ws";

const STRATEGIES = [
  {
    name: "ATR Intraday",
    tag: "1",
    symbols: ["NIFTY"],
    timeframe: "5m",
    status: "active",
    description: "VWAP + ORB + PDH/PDL + SMA/EMA/RSI/MACD. Score -10 to +10, threshold ≥6. Claude AI confirms entry.",
    target: "+298%",
    rr: "1:3.0",
    risk: "2%",
    color: "indigo",
  },
  {
    name: "ICT — OB + Sweep",
    tag: "C",
    symbols: ["NIFTY"],
    timeframe: "5m",
    status: "active",
    description: "Delta direction + Trendline channel (HPS-T) + ICT Order Blocks & Liquidity Sweeps. Best WR 52.8%, lowest DD 6.7% across 90-day backtest.",
    target: "+365%",
    rr: "1:2.5",
    risk: "2%",
    color: "violet",
  },
  {
    name: "Fib-OF",
    tag: "F",
    symbols: ["NIFTY"],
    timeframe: "15m",
    status: "active",
    description: "Fibonacci retracement zones + order flow confirmation on 15m bars. Intraday only. Backtest Jan–Mar 2026: +26.7% at R:R 1:3.",
    target: "+26.7%",
    rr: "1:3.0",
    risk: "2%",
    color: "emerald",
  },
];

export default function Home() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem("aq_token")) {
      router.replace("/login");
    } else {
      setAuthed(true);
    }
  }, []);

  const { data, connected } = useWebSocket(authed ? WS_URL : "");
  const todayStr = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD
  const trades            = (data?.recent_activity ?? data?.round_trips ?? []).filter(
    (t: any) => (t.entry_time || t.timestamp || "").startsWith(todayStr)
  );
  const errorCount        = data?.angel_error_count ?? 0;
  const openPos           = data?.open_positions    ?? [];
  const mode              = data?.mode              ?? "paper";
  const botStatus         = data?.bot_status        ?? "unknown";
  const schedulerRunning  = data?.scheduler_running ?? null;
  const prices            = data?.prices            ?? {};
  const strategySummary   = data?.strategy_summary  ?? {};
  const todayJournal      = data?.today_journal     ?? [];
  const indiaVix          = null;
  const vixBlocked        = false;
  const anyOverride       = false;
  const tokenStatus       = data?.token_set_at       ?? null;
  const tokenLive         = tokenStatus?.live        ?? false;
  const tokenSetAt        = tokenStatus?.set_at      ?? null;
  const latestOrderIssue  = data?.latest_order_issue ?? null;
  const dayBiasData       = data?.day_bias          ?? { bias: "NEUTRAL", note: "", set_at: null };
  const settingsData      = data?.settings          ?? { min_lots: 1 };
  const optionChain       = data?.option_chain      ?? null;

  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const vixOverrideSaving = false;
  const [biasSaving, setBiasSaving]   = useState(false);
  const [lotsSaving, setLotsSaving]   = useState(false);
  const [lotsValue, setLotsValue]     = useState<number>(1);

  // Sync lots from websocket
  useEffect(() => {
    if (settingsData?.min_lots !== undefined) {
      setLotsValue(settingsData.min_lots);
    }
  }, [settingsData?.min_lots]);
  const [biasEdit, setBiasEdit]       = useState(false);
  const [biasNote, setBiasNote]       = useState("");
  const [savedBias, setSavedBias]     = useState<string | null>(null);
  const [parseFlash, setParseFlash]   = useState<{type: string; msg: string} | null>(null);

  // Sync note from websocket when not in edit mode
  useEffect(() => {
    if (!biasEdit && dayBiasData.note !== undefined) {
      setBiasNote(dayBiasData.note);
    }
  }, [dayBiasData.note, biasEdit]);

  async function toggleVixOverride() {
    return;
  }

  async function saveBias(bias: string, note: string) {
    setBiasSaving(true);
    setParseFlash(null);
    try {
      const token = localStorage.getItem("aq_token");
      const res = await fetch(`${API_URL}/api/bot/bias`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ bias, note }),
      });
      const json = await res.json();
      const p = json.parsed;
      if (p && p.type !== "none" && p.explanation) {
        setParseFlash({ type: p.type, msg: p.explanation });
        setTimeout(() => setParseFlash(null), 8000);
      }
      setSavedBias(bias);
      setBiasEdit(false);
      setTimeout(() => setSavedBias(null), 2000);
    } finally {
      setBiasSaving(false);
    }
  }

  async function saveLots(n: number) {
    setLotsSaving(true);
    setLotsValue(n);
    try {
      const token = localStorage.getItem("aq_token");
      await fetch(`${API_URL}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ min_lots: n }),
      });
    } finally {
      setLotsSaving(false);
    }
  }

  if (!authed) return null;

  function handleBotToggle() {
    // WebSocket will auto-update on next push
  }

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode={mode} connected={connected} botStatus={botStatus} onBotToggle={handleBotToggle} errorCount={errorCount} />

      {/* Main — full width */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto p-3 md:p-5">

          {/* Section header with live prices */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 gap-2">
            <div className="w-full sm:w-auto">
              <div className="flex flex-wrap items-center gap-1.5">
                <h2 className="text-base font-bold text-gray-900 w-full sm:w-auto mb-1 sm:mb-0">Live Trade Feed</h2>
                {/* Bot active/inactive indicator */}
                {botStatus === "running" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#dcfce7", color: "#15803d" }}>
                    <span className="w-1.5 h-1.5 rounded-full animate-pulse inline-block"
                          style={{ background: "#22c55e" }} />
                    BOT ACTIVE
                  </span>
                ) : botStatus === "market_closed" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#f3f4f6", color: "#6b7280" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block"
                          style={{ background: "#9ca3af" }} />
                    MARKET CLOSED
                  </span>
                ) : botStatus === "paused" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#fef3c7", color: "#b45309" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block"
                          style={{ background: "#f59e0b" }} />
                    BOT PAUSED
                  </span>
                ) : botStatus === "stopped" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#fee2e2", color: "#dc2626" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block"
                          style={{ background: "#ef4444" }} />
                    BOT STOPPED
                  </span>
                ) : null}
                {/* India VIX badge + override toggle */}
                {indiaVix !== null && (
                  <div className="flex items-center gap-1">
                    <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                          style={vixBlocked
                            ? { background: "#fee2e2", color: "#dc2626" }
                            : anyOverride
                            ? { background: "#fef3c7", color: "#b45309" }
                            : { background: "#f0fdf4", color: "#15803d" }}>
                      VIX {indiaVix.toFixed(1)}
                      {vixBlocked  && <span className="ml-0.5">⛔</span>}
                      {anyOverride && <span className="ml-0.5">⚡</span>}
                    </span>
                    <button
                      onClick={toggleVixOverride}
                      disabled={vixOverrideSaving}
                      title={anyOverride ? "VIX gate bypassed — click to restore" : "VIX gate active — click to bypass"}
                      className="text-[10px] font-bold px-2 py-0.5 rounded-full border transition-all disabled:opacity-50"
                      style={anyOverride
                        ? { background: "#fef3c7", color: "#b45309", borderColor: "#f59e0b" }
                        : { background: "#f3f4f6", color: "#6b7280", borderColor: "#d1d5db" }}>
                      {anyOverride ? "VIX Override ON" : "VIX Override OFF"}
                    </button>
                  </div>
                )}
                {/* Mode badge */}
                <span className="text-[10px] font-bold px-2 py-0.5 rounded-full"
                      style={mode === "live"
                        ? { background: "#fee2e2", color: "#dc2626" }
                        : { background: "#dbeafe", color: "#1d4ed8" }}>
                  {mode === "live" ? "● LIVE" : "● PAPER"}
                </span>
                {/* Angel One session badge */}
                {data && (
                  tokenLive ? (
                    <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                          style={{ background: "#f0fdf4", color: "#15803d" }}>
                      <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#22c55e" }} />
                      TOKEN LIVE{tokenSetAt ? ` ${new Date(tokenSetAt).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}` : ""}
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                          style={{ background: "#fee2e2", color: "#dc2626" }}>
                      <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#ef4444" }} />
                      TOKEN EXPIRED
                    </span>
                  )
                )}
              </div>
              <p className="text-xs text-gray-400">Updates every 5 seconds via WebSocket</p>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {/* Live prices */}
              {Object.entries(prices).map(([sym, q]: any) => {
                const up = q.change_pct >= 0;
                return (
                  <div key={sym} className="flex items-center gap-2">
                    <span className="text-xs font-bold text-gray-500">{sym}</span>
                    <span className="text-sm font-bold text-gray-900">
                      {q.price ? `₹${Number(q.price).toLocaleString("en-IN")}` : "—"}
                    </span>
                    <span className="text-xs font-semibold px-2 py-0.5 rounded-full"
                          style={{ background: up ? "#dcfce7" : "#fee2e2", color: up ? "#16a34a" : "#dc2626" }}>
                      {up ? "▲" : "▼"} {Math.abs(q.change_pct)}%
                    </span>
                  </div>
                );
              })}
              <div className="flex items-center gap-1.5 text-xs text-gray-400">
                <span className={`w-2 h-2 rounded-full ${connected ? "bg-green-500 animate-pulse" : "bg-red-400"}`} />
                {data?.timestamp ? new Date(data.timestamp).toLocaleTimeString("en-IN") : "—"}
              </div>
            </div>
          </div>

          {latestOrderIssue && (
            <div className="mb-4 bg-red-50 border border-red-200 rounded-xl p-3">
              <div className="text-[10px] font-bold uppercase tracking-widest text-red-500 mb-1">Latest Live Order Issue</div>
              <div className="text-sm font-semibold text-red-700">{latestOrderIssue.error}</div>
              <div className="text-xs text-red-500 mt-1">
                {latestOrderIssue.symbol || "—"} {latestOrderIssue.detail ? `· ${latestOrderIssue.detail}` : ""}
                {latestOrderIssue.timestamp ? ` · ${new Date(latestOrderIssue.timestamp).toLocaleTimeString("en-IN")}` : ""}
              </div>
            </div>
          )}

          {/* Strategy P&L + Day Bias row */}
          <div className="flex flex-col md:flex-row gap-4 mb-4 items-start">
            {/* Today's Strategy P&L — always show both cards */}
            <div className="w-full md:flex-1">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Today's Strategy P&L</div>
              <div className="grid grid-cols-3 gap-3">
                {(["ATR Intraday", "C-ICT", "Fib-OF"] as const).map(name => {
                  const s: any = strategySummary[name] ?? { pnl: 0, trades: 0, wins: 0, losses: 0 };
                  return (
                    <div key={name} className="bg-white rounded-xl border border-gray-200 p-3">
                      <div className="text-xs font-bold text-gray-700 mb-2">{name}</div>
                      <div className="text-lg font-bold" style={{ color: s.pnl > 0 ? "#16a34a" : s.pnl < 0 ? "#ef4444" : "#374151" }}>
                        {s.pnl > 0 ? "+" : ""}₹{s.pnl.toLocaleString("en-IN")}
                      </div>
                      <div className="flex gap-3 mt-1 text-xs text-gray-400">
                        <span>{s.trades} trades</span>
                        <span className="text-green-600">{s.wins}W</span>
                        <span className="text-red-500">{s.losses}L</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Day Bias panel */}
            <div className="w-full md:flex-1 min-w-0">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Day Bias</div>
              <div className="bg-white rounded-xl border border-gray-200 p-3 space-y-3">
                {/* Bias buttons */}
                <div className="flex gap-2">
                  {(["BULLISH", "NEUTRAL", "BEARISH"] as const).map(b => {
                    const active = !!dayBiasData.set_at && dayBiasData.bias === b;
                    const cfg = {
                      BULLISH: { active: "bg-green-500 text-white border-green-500", idle: "bg-white text-gray-700 border-gray-300 hover:bg-green-50 hover:border-green-400 hover:text-green-700" },
                      NEUTRAL: { active: "bg-gray-600 text-white border-gray-600",   idle: "bg-white text-gray-700 border-gray-300 hover:bg-gray-100 hover:border-gray-400" },
                      BEARISH: { active: "bg-red-500 text-white border-red-500",     idle: "bg-white text-gray-700 border-gray-300 hover:bg-red-50 hover:border-red-400 hover:text-red-700" },
                    }[b];
                    return (
                      <button key={b} onClick={() => saveBias(b, biasNote)} disabled={biasSaving}
                              className={`flex-1 text-xs font-bold py-2 px-1 sm:px-3 rounded-lg border transition-all ${active ? cfg.active : cfg.idle}`}>
                        {b === "BULLISH" ? "▲ Bullish" : b === "BEARISH" ? "▼ Bearish" : "— Neutral"}
                      </button>
                    );
                  })}
                </div>

                {/* Note textarea */}
                <div className="relative">
                  <textarea
                    rows={3}
                    value={biasNote}
                    onChange={e => { setBiasNote(e.target.value); setBiasEdit(true); }}
                    placeholder="Add note e.g. FII selling, wait for reversal... or type: BUY CE 22500 SL 50 TP 150"
                    className="w-full text-xs text-gray-700 border border-gray-200 rounded-lg px-2.5 py-2 resize-none focus:outline-none focus:border-indigo-300 placeholder-gray-300"
                  />
                  {biasEdit && (
                    <div className="flex gap-1.5 mt-1">
                      <button onClick={() => saveBias(dayBiasData.bias, biasNote)} disabled={biasSaving}
                              className="flex-1 text-[11px] font-bold py-1 rounded-lg bg-indigo-500 text-white hover:bg-indigo-600 transition-colors disabled:opacity-50">
                        {biasSaving ? "Saving…" : "Save"}
                      </button>
                      <button onClick={() => { setBiasNote(dayBiasData.note || ""); setBiasEdit(false); }}
                              className="flex-1 text-[11px] font-bold py-1 rounded-lg border border-gray-200 text-gray-500 hover:border-gray-400 transition-colors">
                        Cancel
                      </button>
                    </div>
                  )}
                </div>

                {/* Parse flash */}
                {parseFlash && (
                  <div className={`text-[11px] px-2.5 py-2 rounded-lg leading-snug ${
                    parseFlash.type === "force_trade" ? "bg-green-50 text-green-700 border border-green-200" :
                    parseFlash.type === "bias"        ? "bg-blue-50 text-blue-700 border border-blue-200" :
                    parseFlash.type === "unclear"     ? "bg-red-50 text-red-600 border border-red-200" :
                    "bg-gray-50 text-gray-500"
                  }`}>
                    {parseFlash.type === "force_trade" && <span className="font-bold">✓ </span>}
                    {parseFlash.type === "bias"        && <span className="font-bold">→ </span>}
                    {parseFlash.type === "unclear"     && <span className="font-bold">✗ </span>}
                    {parseFlash.msg}
                  </div>
                )}

                {/* Status line */}
                <div className="text-[10px] text-gray-400">
                  {savedBias ? (
                    <span className="text-green-600 font-semibold">Saved — bot updated</span>
                  ) : dayBiasData.set_at ? (
                    <>Set {new Date(dayBiasData.set_at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}</>
                  ) : "Not set today"}
                </div>
              </div>
            </div>
          </div>

          {/* Option Chain Bias panel */}
          {optionChain && !optionChain.error && (
            <div className="bg-white rounded-xl p-4 shadow-sm mb-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Option Chain</span>
                  {/* Bias badge */}
                  <span className={`text-sm font-bold px-3 py-1 rounded-full ${
                    optionChain.bias === "CE_FAVORED"
                      ? "bg-green-100 text-green-700"
                      : optionChain.bias === "PE_FAVORED"
                      ? "bg-red-100 text-red-700"
                      : "bg-gray-100 text-gray-600"
                  }`}>
                    {optionChain.bias === "CE_FAVORED" ? "↑ CE FAVORED"
                     : optionChain.bias === "PE_FAVORED" ? "↓ PE FAVORED"
                     : "→ NEUTRAL"}
                  </span>
                </div>
                {/* Key metrics row */}
                <div className="flex flex-wrap gap-4 text-xs text-gray-600">
                  <span>PCR <strong className={optionChain.pcr > 1.1 ? "text-green-600" : optionChain.pcr < 0.9 ? "text-red-600" : "text-gray-800"}>{optionChain.pcr?.toFixed(2)}</strong></span>
                  <span>Max Pain <strong>{optionChain.max_pain?.toLocaleString()}</strong></span>
                  <span>CE Wall <strong className="text-red-600">{optionChain.ce_wall?.toLocaleString()}</strong></span>
                  <span>PE Wall <strong className="text-green-600">{optionChain.pe_wall?.toLocaleString()}</strong></span>
                  <span className="text-gray-400">ATM {optionChain.atm?.toLocaleString()}</span>
                </div>
              </div>

              {/* Mini option chain table */}
              {optionChain.strikes && optionChain.strikes.length > 0 && (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-100">
                        <th className="text-right py-1 pr-2 font-medium">CE OI</th>
                        <th className="text-right py-1 pr-2 font-medium">CE LTP</th>
                        <th className="text-center py-1 px-2 font-semibold text-gray-700">Strike</th>
                        <th className="text-left py-1 pl-2 font-medium">PE LTP</th>
                        <th className="text-left py-1 pl-2 font-medium">PE OI</th>
                      </tr>
                    </thead>
                    <tbody>
                      {optionChain.strikes.map((row: any) => {
                        const isAtm = row.strike === optionChain.atm;
                        const isCeWall = row.strike === optionChain.ce_wall;
                        const isPeWall = row.strike === optionChain.pe_wall;
                        const isMaxPain = row.strike === optionChain.max_pain;
                        const maxOi = Math.max(...optionChain.strikes.map((s: any) => Math.max(s.ce_oi || 0, s.pe_oi || 0)));
                        return (
                          <tr key={row.strike} className={`border-b border-gray-50 ${isAtm ? "bg-blue-50 font-semibold" : ""}`}>
                            <td className="text-right py-1 pr-2 text-red-500">
                              <span style={{opacity: maxOi > 0 ? 0.4 + 0.6 * (row.ce_oi / maxOi) : 1}}>
                                {row.ce_oi > 0 ? (row.ce_oi / 1e5).toFixed(1) + "L" : "—"}
                              </span>
                              {isCeWall && <span className="ml-1 text-[9px] text-red-400 font-bold">WALL</span>}
                            </td>
                            <td className="text-right py-1 pr-2 text-red-600">{row.ce_ltp > 0 ? row.ce_ltp.toFixed(1) : "—"}</td>
                            <td className={`text-center py-1 px-2 font-bold ${isAtm ? "text-blue-700" : "text-gray-700"}`}>
                              {row.strike.toLocaleString()}
                              {isMaxPain && <span className="ml-1 text-[9px] text-orange-500">MP</span>}
                            </td>
                            <td className="text-left py-1 pl-2 text-green-600">{row.pe_ltp > 0 ? row.pe_ltp.toFixed(1) : "—"}</td>
                            <td className="text-left py-1 pl-2 text-green-500">
                              {row.pe_oi > 0 ? (row.pe_oi / 1e5).toFixed(1) + "L" : "—"}
                              {isPeWall && <span className="ml-1 text-[9px] text-green-400 font-bold">WALL</span>}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <p className="text-[10px] text-gray-400 mt-1">
                    ATM = blue · MP = max pain · WALL = highest OI · OI in Lakhs
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Today's trade journal */}
          {todayJournal.length > 0 && (
            <div className="mb-4">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Today's Trade Journal</div>
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden overflow-x-auto">
                <table className="w-full text-xs min-w-[700px]">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-100">
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Strategy</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Option</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Strike</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Entry ₹</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Lots</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">P&L</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Close</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Score</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Entry Time</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Exit Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {todayJournal.map((t: any, i: number) => {
                      const pnl = t.pnl ?? 0;
                      const optColor = t.option_type === "CE" ? "bg-blue-100 text-blue-700" : "bg-orange-100 text-orange-700";
                      return (
                        <>
                        <tr key={`${i}-row`} className="border-b border-gray-50 hover:bg-gray-50">
                          <td className="px-3 py-2 font-semibold text-gray-700">{t.strategy}</td>
                          <td className="px-3 py-2">
                            <span className={`px-1.5 py-0.5 rounded font-bold ${optColor}`}>{t.option_type}</span>
                          </td>
                          <td className="px-3 py-2 text-right text-gray-700">{t.strike ?? "—"}</td>
                          <td className="px-3 py-2 text-right text-gray-700">
                            {t.entry_price ? `₹${Number(t.entry_price).toLocaleString("en-IN")}` : "—"}
                          </td>
                          <td className="px-3 py-2 text-right text-gray-500">{t.lot_size ?? 65}</td>
                          <td className={`px-3 py-2 text-right font-bold ${pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                            {pnl >= 0 ? "+" : ""}₹{pnl.toLocaleString("en-IN")}
                          </td>
                          <td className="px-3 py-2">
                            <span className={`px-1.5 py-0.5 rounded font-medium ${
                              t.close_reason === "TP" ? "bg-green-100 text-green-700" :
                              t.close_reason === "SL" ? "bg-red-100 text-red-700" :
                              "bg-gray-100 text-gray-500"
                            }`}>{t.close_reason ?? "—"}</span>
                          </td>
                          <td className="px-3 py-2 text-gray-500">{t.score ? t.score.toFixed(1) : "—"}</td>
                          <td className="px-3 py-2 text-gray-400">
                            {t.entry_time ? new Date(t.entry_time).toLocaleTimeString("en-IN") : "—"}
                          </td>
                          <td className="px-3 py-2 text-gray-400">
                            {t.exit_time ? new Date(t.exit_time).toLocaleTimeString("en-IN") : "—"}
                          </td>
                        </tr>
                        {(t.entry_remark || t.exit_remark) && (
                          <tr key={`${i}-remarks`} className="border-b border-gray-100 bg-gray-50/50">
                            <td colSpan={10} className="px-4 py-2 space-y-1">
                              {t.entry_remark && (
                                <div className="flex gap-2 text-xs">
                                  <span className="font-bold text-indigo-500 shrink-0">📝 Entry:</span>
                                  <span className="text-gray-600">{t.entry_remark}</span>
                                </div>
                              )}
                              {t.exit_remark && (
                                <div className="flex gap-2 text-xs">
                                  <span className="font-bold text-amber-500 shrink-0">🔍 Review:</span>
                                  <span className="text-gray-600">{t.exit_remark}</span>
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                        </>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Recent trades: open entries first, then completed exits */}
          <div>
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Recent Trades</div>
            {trades.length === 0 ? (
              <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
                No trades yet today. Bot is watching the market.
              </div>
            ) : (
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden overflow-x-auto">
                <table className="w-full text-sm min-w-[700px]">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-100">
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Symbol / Strike</th>
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Strategy</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">Buy Price</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">Sell Price</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">Qty</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">P&L</th>
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Entry → Exit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t: any, i: number) => {
                      const pnl = t.pnl ?? 0;
                      const expiryFmt = t.expiry
                        ? (() => { const d = new Date(t.expiry); return `${String(d.getUTCDate()).padStart(2,"0")} ${d.toLocaleString("en-IN",{month:"short",timeZone:"UTC"}).toUpperCase()} ${d.getUTCFullYear()}`; })()
                        : null;
                      const strike = t.strike
                        ? `${t.symbol ?? "NIFTY"}${expiryFmt ? " " + expiryFmt : ""} ${Number(t.strike).toLocaleString("en-IN")} ${t.option_type ?? ""}`.trim()
                        : null;
                      return (
                        <tr key={i} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                          <td className="px-4 py-3">
                            <div className="font-semibold text-indigo-600">{t.symbol}</div>
                            {strike && <div className="text-xs text-gray-400 mt-0.5">{strike}</div>}
                          </td>
                          <td className="px-4 py-3 text-xs text-gray-500">{t.strategy ?? "—"}</td>
                          <td className="px-4 py-3 text-right font-medium" style={{ color: "#16a34a" }}>
                            {t.buy_price != null ? `₹${Number(t.buy_price).toFixed(2)}` : "—"}
                          </td>
                          <td className="px-4 py-3 text-right font-medium" style={{ color: "#ef4444" }}>
                            {t.sell_price != null ? `₹${Number(t.sell_price).toFixed(2)}` : "—"}
                          </td>
                          <td className="px-4 py-3 text-right text-gray-700">{t.qty}</td>
                          <td className="px-4 py-3 text-right font-semibold" style={{ color: pnl > 0 ? "#16a34a" : pnl < 0 ? "#ef4444" : "#6b7280" }}>
                            {typeof pnl === "number" && pnl !== 0 ? `${pnl > 0 ? "+" : ""}₹${pnl.toFixed(2)}` : t.status === "OPEN" ? "OPEN" : "—"}
                          </td>
                          <td className="px-4 py-3 text-xs text-gray-400">
                            <div>{t.entry_time ? new Date(t.entry_time).toLocaleTimeString("en-IN") : "—"}</div>
                            <div>{t.exit_time  ? new Date(t.exit_time).toLocaleTimeString("en-IN")  : ""}</div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function OpenPositionCard({ trade, prices }: { trade: any; prices: any }) {
  // entry is the option premium paid, not the underlying spot price
  const entry    = trade.buy_price ?? trade.price ?? 0;
  const qty      = trade.qty ?? trade.quantity ?? 1;
  const contract = trade.contract_symbol || trade.symbol || "—";
  const underlying = trade.underlying || trade.symbol || "—";
  const spotQ    = prices[underlying] ?? {};
  const spot     = spotQ.price ?? 0;

  return (
    <div className="bg-white rounded-xl border-2 border-indigo-100 p-4 flex items-center justify-between">
      <div className="flex items-center gap-4">
        <div className="w-9 h-9 rounded-lg bg-indigo-50 flex items-center justify-center text-indigo-600 font-bold text-sm">
          {underlying?.[0]}
        </div>
        <div>
          <div className="flex items-center gap-2">
            <span className="font-bold text-gray-900">{underlying}</span>
            <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full font-semibold">OPEN</span>
            {trade.option_type && (
              <span className="text-xs bg-indigo-50 text-indigo-600 px-1.5 py-0.5 rounded font-bold">
                {trade.strike} {trade.option_type}
              </span>
            )}
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            Entry ₹{entry.toLocaleString("en-IN")} · Qty {qty}
            {trade.strategy && <span className="ml-2 text-gray-400">· {trade.strategy}</span>}
          </div>
          <div className="text-[10px] text-gray-400 mt-0.5 font-mono">{contract}</div>
        </div>
      </div>

      <div className="text-right">
        {spot > 0 && (
          <div className="text-sm font-bold text-gray-900">
            NIFTY ₹{spot.toLocaleString("en-IN")}
          </div>
        )}
        <div className="text-xs text-gray-500">
          Prem entry ₹{entry} · {qty} qty
        </div>
        <div className="text-[10px] text-gray-400 mt-0.5">
          {trade.entry_time ? new Date(trade.entry_time).toLocaleTimeString("en-IN") :
           trade.timestamp  ? new Date(trade.timestamp).toLocaleTimeString("en-IN") : ""}
        </div>
      </div>
    </div>
  );
}
