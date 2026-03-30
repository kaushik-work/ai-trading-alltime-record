"use client";
import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { useWebSocket } from "./hooks/useWebSocket";
import Header from "./components/Header";

const WS_URL  = process.env.NEXT_PUBLIC_WS_URL  || "ws://localhost:8000/ws";

const STRATEGIES = [
  {
    name: "ATR Intraday",
    tag: "旧",
    symbols: ["NIFTY"],
    timeframe: "15m",
    status: "active",
    description: "VWAP + ORB + PDH/PDL + 12 candlestick patterns. Score -10 to +10.",
    target: "—",
    rr: "1:2.0",
    risk: "2%",
    color: "indigo",
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
  const trades            = data?.recent_trades     ?? [];
  const openPos           = data?.open_positions    ?? [];
  const mode              = data?.mode              ?? "paper";
  const botStatus         = data?.bot_status        ?? "unknown";
  const schedulerRunning  = data?.scheduler_running ?? null;
  const prices            = data?.prices            ?? {};
  const strategySummary   = data?.strategy_summary  ?? {};
  const todayJournal      = data?.today_journal     ?? [];
  const indiaVix          = data?.india_vix         ?? null;
  const vixBlocked        = data?.vix_blocked       ?? false;
  const vixThreshold      = data?.vix_threshold     ?? 20;
  const tokenSetAt        = data?.token_set_at      ?? null;
  const tokenToday        = tokenSetAt
    ? new Date(tokenSetAt).toDateString() === new Date().toDateString()
    : false;
  const dayBiasData       = data?.day_bias          ?? { bias: "NEUTRAL", note: "", set_at: null };

  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const [biasSaving, setBiasSaving]   = useState(false);
  const [biasEdit, setBiasEdit]       = useState(false);
  const [biasNote, setBiasNote]       = useState("");
  const [savedBias, setSavedBias]     = useState<string | null>(null);
  const [parseFlash, setParseFlash]   = useState<{type: string; msg: string} | null>(null);
  const prevBiasRef = useRef<string>("");

  // Sync note from websocket when not in edit mode
  useEffect(() => {
    if (!biasEdit && dayBiasData.note !== undefined) {
      setBiasNote(dayBiasData.note);
    }
  }, [dayBiasData.note, biasEdit]);

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

  if (!authed) return null;

  // Merge open positions and recent closed trades for live feed
  const liveTrades = [
    ...openPos.map((t: any) => ({ ...t, _live: true })),
    ...trades.filter((t: any) => t.side === "SELL").slice(0, 30),
  ];

  function handleBotToggle() {
    // WebSocket will auto-update on next push
  }

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode={mode} connected={connected} botStatus={botStatus} onBotToggle={handleBotToggle} />

      {/* Main 1:4 split */}
      <div className="flex flex-1 overflow-hidden" style={{ height: "calc(100vh - 100px)" }}>

        {/* ── Left panel — Strategies (1) ── */}
        <div className="w-64 bg-white border-r border-gray-200 flex flex-col overflow-y-auto flex-shrink-0">
          <div className="px-4 py-3 border-b border-gray-100">
            <span className="text-xs font-bold text-gray-500 uppercase tracking-widest">Strategies</span>
          </div>

          <div className="p-3 space-y-2">
            {STRATEGIES.map((s, i) => {
              const active  = s.status === "active";
              const accent  = s.color === "indigo" ? "border-indigo-200 bg-indigo-50/40"
                            : s.color === "amber"  ? "border-amber-200 bg-amber-50/40"
                            : "border-gray-100 bg-gray-50";
              const tagBg   = s.color === "indigo" ? "bg-indigo-100 text-indigo-700"
                            : s.color === "amber"  ? "bg-amber-100 text-amber-700"
                            : "bg-gray-200 text-gray-500";
              const symBg   = s.color === "indigo" ? "bg-indigo-100 text-indigo-600"
                            : s.color === "amber"  ? "bg-amber-100 text-amber-700"
                            : "bg-gray-100 text-gray-500";
              return (
                <div key={i} className={`rounded-xl border p-3 ${accent}`}>
                  {/* Header row */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${tagBg}`}>{s.tag}</span>
                      <span className="text-sm font-bold text-gray-800">{s.name}</span>
                    </div>
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                      active ? "bg-green-100 text-green-700" : "bg-gray-200 text-gray-400"
                    }`}>
                      {active ? "● LIVE" : "LEGACY"}
                    </span>
                  </div>

                  {/* Symbols + timeframe */}
                  <div className="flex flex-wrap items-center gap-1 mb-2">
                    {s.symbols.map(sym => (
                      <span key={sym} className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${symBg}`}>
                        {sym}
                      </span>
                    ))}
                    <span className="text-[10px] text-gray-400 font-medium ml-1">{s.timeframe}</span>
                  </div>

                  {/* Stats row */}
                  {active && (
                    <div className="flex gap-3 mb-2">
                      <div className="flex-1 bg-white/70 rounded-lg px-2 py-1 text-center">
                        <div className="text-[9px] text-gray-400 uppercase font-semibold">Target</div>
                        <div className="text-[11px] font-bold text-green-600">{s.target}</div>
                      </div>
                      <div className="flex-1 bg-white/70 rounded-lg px-2 py-1 text-center">
                        <div className="text-[9px] text-gray-400 uppercase font-semibold">R:R</div>
                        <div className="text-[11px] font-bold text-gray-700">{s.rr}</div>
                      </div>
                      <div className="flex-1 bg-white/70 rounded-lg px-2 py-1 text-center">
                        <div className="text-[9px] text-gray-400 uppercase font-semibold">Risk</div>
                        <div className="text-[11px] font-bold text-gray-700">{s.risk}</div>
                      </div>
                    </div>
                  )}

                  <div className="text-[10px] text-gray-500 leading-relaxed">{s.description}</div>
                </div>
              );
            })}

            {/* Locked: Swing strategies */}
            <div className="rounded-xl border border-dashed border-gray-200 p-3 text-center">
              <div className="text-xs text-gray-400 font-medium">⚔️ Swing strategies</div>
              <div className="text-[10px] text-gray-300 mt-0.5">unlock at ₹5L profit</div>
            </div>
          </div>
        </div>

        {/* ── Right panel — Live Trades (4) ── */}
        <div className="flex-1 overflow-y-auto p-5">

          {/* Section header with live prices */}
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-gray-900">Live Trade Feed</h2>
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
                {/* India VIX badge */}
                {indiaVix !== null && (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={vixBlocked
                          ? { background: "#fee2e2", color: "#dc2626" }
                          : { background: "#f0fdf4", color: "#15803d" }}>
                    VIX {indiaVix.toFixed(1)}
                    {vixBlocked && <span className="ml-0.5">⛔</span>}
                  </span>
                )}
                {/* Zerodha token badge */}
                {data && (
                  tokenToday ? (
                    <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                          style={{ background: "#f0fdf4", color: "#15803d" }}>
                      <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#22c55e" }} />
                      TOKEN LIVE {new Date(tokenSetAt!).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}
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
            <div className="flex items-center gap-4">
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

          {/* Open positions — highlighted */}
          {openPos.length > 0 && (
            <div className="mb-4">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Open Positions</div>
              <div className="space-y-2">
                {openPos.map((t: any, i: number) => (
                  <OpenPositionCard key={i} trade={t} prices={prices} />
                ))}
              </div>
            </div>
          )}

          {/* Strategy P&L + Day Bias row */}
          <div className="flex gap-4 mb-4 items-start">
            {/* Today's Strategy P&L */}
            {Object.keys(strategySummary).length > 0 && (
              <div className="flex-1">
                <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Today's Strategy P&L</div>
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(strategySummary).map(([name, s]: any) => (
                    <div key={name} className="bg-white rounded-xl border border-gray-200 p-3">
                      <div className="text-xs font-bold text-gray-700 mb-2">{name}</div>
                      <div className={`text-lg font-bold ${s.pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                        {s.pnl >= 0 ? "+" : ""}₹{s.pnl.toLocaleString("en-IN")}
                      </div>
                      <div className="flex gap-3 mt-1 text-xs text-gray-400">
                        <span>{s.trades} trades</span>
                        <span className="text-green-600">{s.wins}W</span>
                        <span className="text-red-500">{s.losses}L</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Day Bias panel */}
            <div className="w-72 flex-shrink-0">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Day Bias</div>
              <div className="bg-white rounded-xl border border-gray-200 p-3 space-y-3">
                {/* Bias buttons */}
                <div className="flex gap-2">
                  {(["BULLISH", "NEUTRAL", "BEARISH"] as const).map(b => {
                    const active = dayBiasData.bias === b;
                    const cfg = {
                      BULLISH: { active: "bg-green-500 text-white border-green-500", idle: "border-gray-200 text-gray-500 hover:border-green-300" },
                      NEUTRAL: { active: "bg-gray-500 text-white border-gray-500", idle: "border-gray-200 text-gray-500 hover:border-gray-400" },
                      BEARISH: { active: "bg-red-500 text-white border-red-500",   idle: "border-gray-200 text-gray-500 hover:border-red-300" },
                    }[b];
                    return (
                      <button key={b} onClick={() => saveBias(b, biasNote)} disabled={biasSaving}
                              className={`flex-1 text-[11px] font-bold py-1.5 rounded-lg border transition-all ${active ? cfg.active : cfg.idle}`}>
                        {b === "BULLISH" ? "▲ Bull" : b === "BEARISH" ? "▼ Bear" : "— Neutral"}
                      </button>
                    );
                  })}
                </div>

                {/* Note textarea */}
                <div className="relative">
                  <textarea
                    rows={2}
                    value={biasNote}
                    onChange={e => { setBiasNote(e.target.value); setBiasEdit(true); }}
                    placeholder="Add note e.g. FII selling, wait for reversal..."
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

          {/* Today's trade journal */}
          {todayJournal.length > 0 && (
            <div className="mb-4">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Today's Trade Journal</div>
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
                <table className="w-full text-xs">
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
                          <td className="px-3 py-2 text-right text-gray-500">{t.lot_size ?? 75}</td>
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

          {/* Recent closed trades */}
          <div>
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Recent Trades</div>
            {trades.length === 0 ? (
              <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
                No trades yet today. Bot is watching the market.
              </div>
            ) : (
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-100">
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Symbol</th>
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Side</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">Price</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">Qty</th>
                      <th className="px-4 py-3 text-right text-xs text-gray-500 font-semibold uppercase">P&L</th>
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Status</th>
                      <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t: any, i: number) => {
                      const pnl = t.pnl ?? 0;
                      return (
                        <tr key={i} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                          <td className="px-4 py-3 font-semibold text-indigo-600">{t.symbol}</td>
                          <td className={`px-4 py-3 font-semibold ${t.side === "BUY" ? "text-green-600" : "text-red-500"}`}>
                            {t.side}
                          </td>
                          <td className="px-4 py-3 text-right text-gray-700">₹{Number(t.price).toLocaleString("en-IN")}</td>
                          <td className="px-4 py-3 text-right text-gray-700">{t.quantity}</td>
                          <td className={`px-4 py-3 text-right font-semibold ${pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                            {pnl !== 0 ? `₹${pnl.toFixed(2)}` : "—"}
                          </td>
                          <td className="px-4 py-3">
                            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                              t.status === "COMPLETE" ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
                            }`}>{t.status}</span>
                          </td>
                          <td className="px-4 py-3 text-gray-400 text-xs">
                            {t.timestamp ? new Date(t.timestamp).toLocaleTimeString("en-IN") : "—"}
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
  const q        = prices[trade.symbol] ?? {};
  const current  = q.price ?? 0;
  const entry    = trade.price ?? 0;
  const unreal   = current && entry ? ((current - entry) / entry * 100).toFixed(2) : null;
  const unrealRs = current && entry ? ((current - entry) * (trade.quantity ?? 1)).toFixed(2) : null;
  const isProfit = parseFloat(unreal ?? "0") >= 0;

  return (
    <div className="bg-white rounded-xl border-2 border-indigo-100 p-4 flex items-center justify-between">
      <div className="flex items-center gap-4">
        <div className="w-9 h-9 rounded-lg bg-indigo-50 flex items-center justify-center text-indigo-600 font-bold text-sm">
          {trade.symbol?.[0]}
        </div>
        <div>
          <div className="flex items-center gap-2">
            <span className="font-bold text-gray-900">{trade.symbol}</span>
            <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full font-semibold">OPEN</span>
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            Entry ₹{entry.toLocaleString("en-IN")} · Qty {trade.quantity}
          </div>
        </div>
      </div>

      <div className="text-right">
        <div className="text-sm font-bold text-gray-900">
          {current ? `₹${current.toLocaleString("en-IN")}` : "—"}
        </div>
        {unreal && (
          <div className={`text-xs font-semibold ${isProfit ? "text-green-600" : "text-red-500"}`}>
            {isProfit ? "▲" : "▼"} {Math.abs(parseFloat(unreal))}% · ₹{unrealRs}
          </div>
        )}
        <div className="text-[10px] text-gray-400 mt-0.5">
          {trade.timestamp ? new Date(trade.timestamp).toLocaleTimeString("en-IN") : ""}
        </div>
      </div>
    </div>
  );
}
