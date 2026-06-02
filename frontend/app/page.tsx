"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useWebSocket } from "./hooks/useWebSocket";
import Header from "./components/Header";
import MongoBadge from "./components/MongoBadge";
import ShadowBadge from "./components/ShadowBadge";
import NiftyChart from "./components/NiftyChart";
import StrategyExplainer from "./components/StrategyExplainer";

const _API   = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL = _API.replace(/^http/, "ws") + "/ws";

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
  const errorCount       = data?.angel_error_count ?? 0;
  const mode             = data?.mode              ?? "shadow";
  const botStatus        = data?.bot_status        ?? "unknown";
  const prices           = data?.prices            ?? {};
  const tokenStatus      = data?.token_set_at       ?? null;
  const tokenLive        = tokenStatus?.live        ?? false;
  const tokenSetAt       = tokenStatus?.set_at      ?? null;
  const latestOrderIssue = data?.latest_order_issue ?? null;
  const settingsData     = data?.settings          ?? { min_lots: 1 };
  const optionChain      = data?.option_chain      ?? null;
  const shadow           = data?.shadow            ?? null;

  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  // Shadow trade ledger — fetched separately from /api/shadow-trades
  const [shadowLedger, setShadowLedger] = useState<any>(null);
  useEffect(() => {
    if (!authed) return;
    let cancel = false;
    async function load() {
      const token = localStorage.getItem("aq_token");
      if (!token) return;
      try {
        const r = await fetch(`${API_URL}/api/shadow-trades?days=7`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!r.ok) return;
        const j = await r.json();
        if (!cancel) setShadowLedger(j);
      } catch { /* ignore */ }
    }
    load();
    const t = setInterval(load, 60_000);
    return () => { cancel = true; clearInterval(t); };
  }, [authed]);

  if (!authed) return null;

  function handleBotToggle() {
    // WebSocket will auto-update on next push
  }

  // Today-only shadow trades for the table
  const todayStr = new Date().toLocaleDateString("en-CA");
  const allTrades: any[] = shadowLedger?.strategies
    ? Object.values(shadowLedger.strategies).flatMap((s: any) => s.trades || [])
    : [];
  const todayTrades = allTrades
    .filter(t => (t.date || "").startsWith(todayStr))
    .sort((a, b) => (b.entry_dt || "").localeCompare(a.entry_dt || ""));

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header
        mode={mode}
        connected={connected}
        botStatus={botStatus}
        onBotToggle={handleBotToggle}
        errorCount={errorCount}
        settings={settingsData}
      />

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto p-3 md:p-5">

          {/* Switch to crypto dashboard */}
          <div className="flex justify-end mb-3">
            <button
              onClick={() => router.push("/crypto")}
              className="px-4 py-2 text-xs font-medium rounded-lg text-white shadow-sm hover:opacity-90 transition-opacity"
              style={{ background: "linear-gradient(135deg,#f7931a 0%,#627eea 100%)" }}
            >
              Switch to Crypto →
            </button>
          </div>

          {/* Strategy explainer — what's running */}
          <StrategyExplainer />

          {/* Live NIFTY chart */}
          <div className="mb-5">
            <NiftyChart livePrice={prices?.NIFTY?.price ?? undefined} />
          </div>

          {/* Status chips + live prices */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 gap-2">
            <div className="w-full sm:w-auto">
              <div className="flex flex-wrap items-center gap-1.5">
                <h2 className="text-base font-bold text-gray-900 w-full sm:w-auto mb-1 sm:mb-0">
                  Shadow Trading
                </h2>

                {/* Bot scheduler state */}
                {botStatus === "running" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#dcfce7", color: "#15803d" }}
                        title="The bot scheduler is running cycles">
                    <span className="w-1.5 h-1.5 rounded-full animate-pulse inline-block"
                          style={{ background: "#22c55e" }} />
                    BOT RUNNING
                  </span>
                ) : botStatus === "market_closed" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#f3f4f6", color: "#6b7280" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#9ca3af" }} />
                    MARKET CLOSED
                  </span>
                ) : botStatus === "paused" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#fef3c7", color: "#b45309" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#f59e0b" }} />
                    BOT PAUSED
                  </span>
                ) : botStatus === "stopped" ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        style={{ background: "#fee2e2", color: "#dc2626" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#ef4444" }} />
                    BOT STOPPED
                  </span>
                ) : null}

                {/* Angel One session */}
                {data && (tokenLive ? (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        title="Angel One broker session is valid"
                        style={{ background: "#f0fdf4", color: "#15803d" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#22c55e" }} />
                    ANGEL ✓{tokenSetAt ? ` ${new Date(tokenSetAt).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}` : ""}
                  </span>
                ) : (
                  <span className="flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full"
                        title="Angel One session expired"
                        style={{ background: "#fee2e2", color: "#dc2626" }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: "#ef4444" }} />
                    ANGEL ✗ EXPIRED
                  </span>
                ))}

                <MongoBadge />
                <ShadowBadge />
              </div>
              <p className="text-xs text-gray-400">Forward-testing only — no real orders are placed.</p>
            </div>

            {/* Live prices */}
            <div className="flex flex-wrap items-center gap-3">
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

          {/* Per-strategy shadow summary */}
          {shadow?.strategies && (
            <div className="mb-4">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">
                Shadow strategies — today
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {Object.entries(shadow.strategies).map(([name, s]: any) => (
                  <div key={name} className="bg-white rounded-xl border border-gray-200 p-3">
                    <div className="text-xs font-bold text-gray-700 mb-1">{name}</div>
                    <div className="text-lg font-bold"
                         style={{ color: s.today_pnl > 0 ? "#16a34a" : s.today_pnl < 0 ? "#ef4444" : "#374151" }}>
                      {s.today_pnl > 0 ? "+" : ""}₹{Math.round(s.today_pnl || 0).toLocaleString("en-IN")}
                    </div>
                    <div className="flex gap-3 mt-1 text-xs text-gray-400">
                      <span>{s.trades_today ?? 0} today</span>
                      <span className="text-gray-500">WR {s.win_rate ?? 0}%</span>
                      {s.open && <span className="text-blue-500 font-bold">OPEN</span>}
                    </div>
                    <div className="text-[10px] text-gray-400 mt-1">
                      total {s.total_pnl > 0 ? "+" : ""}₹{Math.round(s.total_pnl || 0).toLocaleString("en-IN")} · {s.closed ?? 0} closed
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Option chain panel (NIFTY ATM ± strikes for context) */}
          {optionChain && !optionChain.error && (
            <div className="bg-white rounded-xl p-4 shadow-sm mb-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Option Chain</span>
                  <span className={`text-sm font-bold px-3 py-1 rounded-full ${
                    optionChain.bias === "CE_FAVORED" ? "bg-green-100 text-green-700"
                    : optionChain.bias === "PE_FAVORED" ? "bg-red-100 text-red-700"
                    : "bg-gray-100 text-gray-600"
                  }`}>
                    {optionChain.bias === "CE_FAVORED" ? "↑ CE FAVORED"
                     : optionChain.bias === "PE_FAVORED" ? "↓ PE FAVORED"
                     : "→ NEUTRAL"}
                  </span>
                </div>
                <div className="flex flex-wrap gap-4 text-xs text-gray-600">
                  <span>PCR <strong>{optionChain.pcr?.toFixed(2)}</strong></span>
                  <span>Max Pain <strong>{optionChain.max_pain?.toLocaleString()}</strong></span>
                  <span>CE Wall <strong className="text-red-600">{optionChain.ce_wall?.toLocaleString()}</strong></span>
                  <span>PE Wall <strong className="text-green-600">{optionChain.pe_wall?.toLocaleString()}</strong></span>
                  <span className="text-gray-400">ATM {optionChain.atm?.toLocaleString()}</span>
                </div>
              </div>
            </div>
          )}

          {/* Today's shadow trades */}
          <div>
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">
              Today's shadow trades
            </div>
            {todayTrades.length === 0 ? (
              <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
                No shadow trades yet today. Bot is watching the market.
              </div>
            ) : (
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden overflow-x-auto">
                <table className="w-full text-xs min-w-[700px]">
                  <thead>
                    <tr className="bg-gray-50 border-b border-gray-100">
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Strategy</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Side</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Strike</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Entry ₹</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">Exit ₹</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Reason</th>
                      <th className="px-3 py-2 text-right text-gray-500 font-semibold uppercase">P&L</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Entry</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">Exit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {todayTrades.map((t: any, i: number) => {
                      const pnl = t.pnl ?? 0;
                      const reasonColor =
                        t.reason === "TP" ? "bg-green-100 text-green-700"
                        : t.reason === "SL" ? "bg-red-100 text-red-700"
                        : t.status === "OPEN" ? "bg-blue-100 text-blue-700"
                        : "bg-gray-100 text-gray-500";
                      return (
                        <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
                          <td className="px-3 py-2 font-semibold text-gray-700">{t.strategy}</td>
                          <td className="px-3 py-2">
                            <span className="px-1.5 py-0.5 rounded font-bold bg-blue-100 text-blue-700">{t.side}</span>
                          </td>
                          <td className="px-3 py-2 text-right text-gray-700">{t.strike}</td>
                          <td className="px-3 py-2 text-right text-gray-700">₹{Number(t.entry_premium).toFixed(2)}</td>
                          <td className="px-3 py-2 text-right text-gray-700">
                            {t.exit_premium != null ? `₹${Number(t.exit_premium).toFixed(2)}` : "—"}
                          </td>
                          <td className="px-3 py-2">
                            <span className={`px-1.5 py-0.5 rounded font-medium ${reasonColor}`}>
                              {t.reason || (t.status === "OPEN" ? "OPEN" : "—")}
                            </span>
                          </td>
                          <td className={`px-3 py-2 text-right font-bold ${pnl > 0 ? "text-green-600" : pnl < 0 ? "text-red-500" : "text-gray-500"}`}>
                            {pnl !== 0 ? `${pnl > 0 ? "+" : ""}₹${Math.round(pnl).toLocaleString("en-IN")}` : t.status === "OPEN" ? "—" : "—"}
                          </td>
                          <td className="px-3 py-2 text-gray-400">
                            {t.entry_dt ? t.entry_dt.slice(11, 19) : "—"}
                          </td>
                          <td className="px-3 py-2 text-gray-400">
                            {t.exit_dt ? t.exit_dt.slice(11, 19) : "—"}
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
