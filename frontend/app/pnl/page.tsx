"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";
import { useWebSocket } from "../hooks/useWebSocket";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL  = API_URL.replace(/^http/, "ws") + "/ws";

function authHeaders() {
  return { Authorization: `Bearer ${localStorage.getItem("aq_token") ?? ""}` };
}

export default function PnLPage() {
  const router = useRouter();
  useEffect(() => {
    if (!localStorage.getItem("aq_token")) router.push("/login");
  }, []);

  const { data: wsData, connected } = useWebSocket(WS_URL);
  const today = new Date().toISOString().split("T")[0];
  const [start, setStart]         = useState(today);
  const [end, setEnd]             = useState(today);
  const [data, setData]           = useState<any>(null);
  const [loading, setLoading]     = useState(false);
  const [expandedDate, setExpand] = useState<string | null>(null);

  async function fetchPnL() {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/pnl?start=${start}&end=${end}`, {
        headers: authHeaders(),
      });
      setData(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchPnL(); }, []);

  return (
    <div className="min-h-screen bg-[#f0f2f5]">
      <Header
        mode={wsData?.mode ?? "paper"}
        connected={connected}
        botStatus={wsData?.bot_status ?? "unknown"}
        onBotToggle={() => {}}
      />

      <div className="max-w-6xl mx-auto p-6 space-y-5">
        <div className="flex items-center gap-3">
          <button onClick={() => router.push("/")} className="text-gray-400 hover:text-gray-700 text-sm font-medium">← Back</button>
          <div className="w-px h-4 bg-gray-200" />
          <span className="font-bold text-gray-800 text-sm">Profit & Loss Report</span>
        </div>

        {/* ── Date filter ── */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <label className="text-xs text-gray-500 mb-1.5 block font-medium uppercase tracking-widest">From</label>
              <input type="date" value={start} onChange={e => setStart(e.target.value)}
                className="aq-input w-40" />
            </div>
            <div>
              <label className="text-xs text-gray-500 mb-1.5 block font-medium uppercase tracking-widest">To</label>
              <input type="date" value={end} onChange={e => setEnd(e.target.value)}
                className="aq-input w-40" />
            </div>
            {/* Quick filters */}
            <div className="flex gap-2 flex-wrap">
              {[
                { label: "Today",    days: 0 },
                { label: "7 Days",   days: 7 },
                { label: "30 Days",  days: 30 },
                { label: "All Time", days: 365 },
              ].map(({ label, days }) => (
                <button key={label} onClick={() => {
                  const s = new Date();
                  s.setDate(s.getDate() - days);
                  setStart(s.toISOString().split("T")[0]);
                  setEnd(today);
                }}
                  className="text-xs px-3 py-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-700 transition-colors font-medium">
                  {label}
                </button>
              ))}
            </div>
            <button onClick={fetchPnL} disabled={loading}
              className="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50">
              {loading ? "Loading..." : "Apply"}
            </button>
          </div>
        </div>

        {/* ── Summary KPIs ── */}
        {data && (
          <div className="grid grid-cols-3 gap-4">
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
              <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1">Total P&L</div>
              <div className={`text-2xl font-bold ${data.total_pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                ₹{data.total_pnl?.toFixed(2)}
              </div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
              <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1">Total Trades</div>
              <div className="text-2xl font-bold text-gray-900">{data.total_trades}</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
              <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1">Win Rate</div>
              <div className="text-2xl font-bold text-gray-900">{data.win_rate}%</div>
            </div>
          </div>
        )}

        {/* ── Daily breakdown ── */}
        {data?.daily?.length > 0 && (
          <div className="space-y-3">
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest">Daily Breakdown</div>

            {data.daily.map((day: any) => (
              <div key={day.date} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">

                {/* Day header — clickable to expand */}
                <button
                  onClick={() => setExpand(expandedDate === day.date ? null : day.date)}
                  className="w-full px-5 py-4 flex items-center justify-between hover:bg-gray-50 transition-colors"
                >
                  <div className="flex items-center gap-4">
                    <span className="font-semibold text-gray-900 text-sm">{day.date}</span>
                    <span className="text-xs text-gray-500">{day.trades.length} trades</span>
                    <span className="text-xs text-green-600">{day.wins}W</span>
                    <span className="text-xs text-red-500">{day.losses}L</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className={`font-bold text-sm ${day.total_pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                      {day.total_pnl >= 0 ? "+" : ""}₹{day.total_pnl.toFixed(2)}
                    </span>
                    <svg className={`w-4 h-4 text-gray-400 transition-transform ${expandedDate === day.date ? "rotate-180" : ""}`}
                         fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </button>

                {/* Trade details — expanded */}
                {expandedDate === day.date && (
                  <div className="border-t border-gray-100 overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50">
                          <th className="px-4 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase">Time</th>
                          <th className="px-4 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase">Symbol</th>
                          <th className="px-4 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase">Side</th>
                          <th className="px-4 py-2.5 text-right text-xs text-gray-500 font-semibold uppercase">Entry</th>
                          <th className="px-4 py-2.5 text-right text-xs text-gray-500 font-semibold uppercase">Exit</th>
                          <th className="px-4 py-2.5 text-right text-xs text-gray-500 font-semibold uppercase">Stop Loss</th>
                          <th className="px-4 py-2.5 text-right text-xs text-gray-500 font-semibold uppercase">Qty</th>
                          <th className="px-4 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase">Timeframe</th>
                          <th className="px-4 py-2.5 text-right text-xs text-gray-500 font-semibold uppercase">P&L</th>
                          <th className="px-4 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase">Result</th>
                        </tr>
                      </thead>
                      <tbody>
                        {day.trades.map((t: any, i: number) => {
                          const pnl = t.pnl ?? 0;
                          const isWin = pnl > 0;
                          const isSell = t.side === "SELL";
                          return (
                            <tr key={i} className="border-t border-gray-50 hover:bg-gray-50 transition-colors">
                              <td className="px-4 py-2.5 text-gray-500 text-xs font-mono">
                                {t.timestamp ? new Date(t.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "—"}
                              </td>
                              <td className="px-4 py-2.5 font-semibold text-indigo-600">{t.symbol}</td>
                              <td className={`px-4 py-2.5 font-semibold text-xs ${t.side === "BUY" ? "text-green-600" : "text-red-500"}`}>
                                {t.side}
                              </td>
                              <td className="px-4 py-2.5 text-right text-gray-700">
                                {t.side === "BUY" ? `₹${Number(t.price).toLocaleString("en-IN")}` : "—"}
                              </td>
                              <td className="px-4 py-2.5 text-right text-gray-700">
                                {isSell ? `₹${Number(t.price).toLocaleString("en-IN")}` : "—"}
                              </td>
                              <td className="px-4 py-2.5 text-right text-orange-500 text-xs font-medium">
                                {t.stop_loss ? `₹${Number(t.stop_loss).toLocaleString("en-IN")}` : "—"}
                              </td>
                              <td className="px-4 py-2.5 text-right text-gray-700">{t.quantity}</td>
                              <td className="px-4 py-2.5 text-xs text-gray-500">{t.timeframe || "15m"}</td>
                              <td className={`px-4 py-2.5 text-right font-bold ${pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                                {pnl !== 0 ? `${pnl >= 0 ? "+" : ""}₹${pnl.toFixed(2)}` : "—"}
                              </td>
                              <td className="px-4 py-2.5">
                                {pnl !== 0 && (
                                  <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                                    isWin ? "bg-green-100 text-green-700" : "bg-red-100 text-red-600"
                                  }`}>
                                    {isWin ? "WIN" : "LOSS"}
                                  </span>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {data && data.daily?.length === 0 && (
          <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
            No trades found for the selected period.
          </div>
        )}
      </div>
    </div>
  );
}
