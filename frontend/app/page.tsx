"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useWebSocket } from "./hooks/useWebSocket";
import Header from "./components/Header";

const WS_URL  = process.env.NEXT_PUBLIC_WS_URL  || "ws://localhost:8000/ws";

const STRATEGIES = [
  {
    name: "Musashi",
    tag: "侍",
    symbols: ["NIFTY"],
    timeframe: "15m",
    status: "active",
    description: "EMA stack + VWAP pullback + HA confirmation + swing structure. 2 trades/day. R:R 1:2.5.",
    target: "30–40% / mo",
    rr: "1:2.5",
    color: "indigo",
  },
  {
    name: "Raijin",
    tag: "雷",
    symbols: ["NIFTY"],
    timeframe: "5m",
    status: "active",
    description: "VWAP ±2σ mean reversion + HA flip + RSI extreme. 3 scalps/day. R:R 1:2.",
    target: "30–40% / mo",
    rr: "1:2.0",
    color: "amber",
  },
  {
    name: "ATR Intraday",
    tag: "旧",
    symbols: ["NIFTY"],
    timeframe: "15m",
    status: "legacy",
    description: "VWAP + ORB + PDH/PDL + 12 candlestick patterns. Score -10 to +10.",
    target: "—",
    rr: "1:2.0",
    color: "gray",
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

  if (!authed) return null;

  const { data, connected } = useWebSocket(WS_URL);
  const trades     = data?.recent_trades   ?? [];
  const openPos    = data?.open_positions  ?? [];
  const mode       = data?.mode            ?? "paper";
  const botStatus  = data?.bot_status      ?? "unknown";
  const prices     = data?.prices          ?? {};

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
                        <div className="text-[11px] font-bold text-gray-700">4%</div>
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
              <h2 className="text-base font-bold text-gray-900">Live Trade Feed</h2>
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
