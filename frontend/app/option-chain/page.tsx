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

export default function OptionChainPage() {
  const router = useRouter();
  useEffect(() => {
    if (!localStorage.getItem("aq_token")) router.push("/login");
  }, []);

  const { data: wsData, connected } = useWebSocket(WS_URL);
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState(2);

  async function fetchLog() {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/pcr-log?days=${days}`, {
        headers: authHeaders(),
      });
      const json = await res.json();
      // Reverse array so newest is at the top
      setData((json || []).reverse());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchLog(); }, [days]);

  return (
    <div className="min-h-screen bg-[#f0f2f5] text-gray-900">
      <Header
        mode={wsData?.mode ?? "paper"}
        connected={connected}
        botStatus={wsData?.bot_status ?? "unknown"}
        onBotToggle={() => {}}
      />

      <div className="max-w-6xl mx-auto p-6 space-y-5 text-gray-900">
        <div className="flex items-center gap-3">
          <button onClick={() => router.push("/")} className="text-gray-400 hover:text-gray-700 text-sm font-medium">← Back</button>
          <div className="w-px h-4 bg-gray-200" />
          <span className="font-bold text-gray-800 text-sm">Option Chain Live Tracking</span>
        </div>

        {/* ── Filters ── */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5 text-gray-900 flex justify-between items-center">
          <div className="flex gap-2 flex-wrap items-center">
            <span className="text-sm font-medium text-gray-600 mr-2">Timeframe:</span>
            {[
              { label: "Today", days: 1 },
              { label: "Last 2 Days", days: 2 },
              { label: "Last 7 Days", days: 7 },
            ].map(({ label, days: d }) => (
              <button key={label} onClick={() => setDays(d)}
                className={`text-xs px-3 py-1.5 rounded-md font-semibold transition-colors
                  ${days === d ? "bg-blue-50 text-blue-600 border border-blue-200" 
                               : "bg-white border border-gray-200 text-gray-600 hover:bg-gray-50"}`}>
                {label}
              </button>
            ))}
          </div>
          <button 
            onClick={fetchLog}
            className="text-xs bg-white border border-gray-200 text-gray-600 px-3 py-1.5 rounded-md font-semibold hover:bg-gray-50 shadow-sm"
          >
            ↻ Refresh
          </button>
        </div>

        {/* ── Data Table ── */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden text-gray-900">
          <div className="p-4 border-b border-gray-100 bg-gray-50/50 flex justify-between items-center">
            <h3 className="font-bold text-gray-800">5-Minute Snapshots</h3>
            <span className="text-xs text-gray-500 font-medium">{data.length} records</span>
          </div>
          
          <div className="overflow-x-auto">
            {loading ? (
              <div className="p-10 text-center text-sm text-gray-500 font-medium">Loading data...</div>
            ) : data.length === 0 ? (
              <div className="p-10 text-center text-sm text-gray-500 font-medium">No option chain data found for the selected timeframe.</div>
            ) : (
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead>
                  <tr className="bg-white border-b border-gray-100 text-gray-500 font-semibold tracking-wider uppercase text-[10px]">
                    <th className="p-3 pl-5">Time</th>
                    <th className="p-3">Symbol</th>
                    <th className="p-3 text-right">PCR</th>
                    <th className="p-3 text-right">CE OI</th>
                    <th className="p-3 text-right">PE OI</th>
                    <th className="p-3 text-right">CE Wall</th>
                    <th className="p-3 text-right">PE Wall</th>
                    <th className="p-3 text-right">Max Pain</th>
                    <th className="p-3 text-right pr-5">Spot Price</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100/80">
                  {data.map((row, i) => {
                    const timeStr = row.timestamp.replace("T", " ").substring(0, 19);
                    const isBullish = row.pcr > 1.0;
                    const isBearish = row.pcr < 0.8;
                    
                    return (
                      <tr key={i} className="hover:bg-gray-50/80 transition-colors group">
                        <td className="p-3 pl-5 text-gray-600 font-medium text-xs tabular-nums">{timeStr}</td>
                        <td className="p-3 font-semibold text-gray-800">{row.symbol}</td>
                        <td className={`p-3 text-right font-bold tabular-nums
                          ${isBullish ? "text-emerald-600" : isBearish ? "text-rose-600" : "text-gray-700"}`}>
                          {row.pcr.toFixed(2)}
                        </td>
                        <td className="p-3 text-right text-rose-600/80 font-medium tabular-nums">
                          {(row.ce_oi / 100000).toFixed(1)}L
                        </td>
                        <td className="p-3 text-right text-emerald-600/80 font-medium tabular-nums">
                          {(row.pe_oi / 100000).toFixed(1)}L
                        </td>
                        <td className="p-3 text-right text-gray-600 font-medium tabular-nums">{row.ce_wall}</td>
                        <td className="p-3 text-right text-gray-600 font-medium tabular-nums">{row.pe_wall}</td>
                        <td className="p-3 text-right text-blue-600 font-medium tabular-nums">{row.max_pain}</td>
                        <td className="p-3 text-right pr-5 font-bold text-gray-900 tabular-nums">
                          {row.spot.toLocaleString('en-IN')}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
