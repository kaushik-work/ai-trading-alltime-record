"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface SignalRow {
  id: number;
  timestamp: string;
  date: string;
  strategy: string;
  symbol: string;
  score: number;
  threshold: number;
  direction: string;
  will_trade: number;
  did_trade: number;
  reason_skipped: string | null;
  nifty_spot: number | null;
  option_type: string | null;
  strike: number | null;
  option_premium: number | null;
  signals_fired: string | null;
  pre2_premium: number | null;
  pre3_premium: number | null;
}

function fmt(ts: string) {
  if (!ts) return "-";
  const d = new Date(ts);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function scoreColor(score: number, threshold: number) {
  const abs = Math.abs(score);
  if (abs >= threshold) return "text-green-600 font-bold";
  if (abs >= threshold * 0.7) return "text-yellow-600 font-semibold";
  return "text-gray-500";
}

function dirBadge(dir: string) {
  if (dir === "BUY")  return <span className="px-1.5 py-0.5 rounded text-xs bg-green-100 text-green-700 font-semibold">BUY</span>;
  if (dir === "SELL") return <span className="px-1.5 py-0.5 rounded text-xs bg-red-100 text-red-700 font-semibold">SELL</span>;
  return <span className="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-500">HOLD</span>;
}

export default function SignalHistoryPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = localStorage.getItem("aq_token");
    if (!token) { router.replace("/login"); return; }
    setAuthed(true);
  }, [router]);

  const fetchSignals = useCallback(async () => {
    const token = localStorage.getItem("aq_token");
    setLoading(true); setError(null);
    try {
      const res = await fetch(`${API_URL}/api/signal-log?date=${date}&limit=200`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(await res.text());
      const d = await res.json();
      setRows(d.rows || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load signals");
    } finally { setLoading(false); }
  }, [date]);

  useEffect(() => {
    if (!authed) return;
    fetchSignals();
  }, [authed, fetchSignals]);

  if (!authed) return null;

  const traded    = rows.filter(r => r.did_trade);
  const fired     = rows.filter(r => r.will_trade && !r.did_trade);
  const skipped   = rows.filter(r => !r.will_trade);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header mode="live" connected={true} botStatus="unknown" onBotToggle={() => {}} />
      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={() => router.back()} className="text-sm text-gray-500 hover:text-gray-800">← Back</button>
          <h1 className="text-xl font-bold text-gray-900">Signal History</h1>
        </div>

        {error && <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error}</div>}

        <div className="flex items-center gap-3 mb-4">
          <input type="date" value={date} onChange={e => setDate(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white" />
          <button onClick={fetchSignals}
            className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">Refresh</button>
          <span className="text-sm text-gray-500">
            {rows.length} evaluations — {traded.length} traded · {fired.length} signal fired (no trade) · {skipped.length} skipped
          </span>
        </div>

        {loading ? (
          <div className="text-center py-12 text-gray-400">Loading...</div>
        ) : rows.length === 0 ? (
          <div className="text-center py-12 text-gray-400">No evaluations logged for {date}. Bot logs signals every 5 min during market hours.</div>
        ) : (
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Time</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Score</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Direction</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">NIFTY</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">ATM Prem</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">-10m Prem</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">-15m Prem</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Top Signals</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Skipped Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.map(row => {
                  const prem = row.option_premium;
                  const pre2 = row.pre2_premium;
                  const pre3 = row.pre3_premium;
                  const diff2 = prem && pre2 ? prem - pre2 : null;
                  const diff3 = prem && pre3 ? prem - pre3 : null;
                  return (
                    <tr key={row.id} className={`hover:bg-gray-50 ${row.did_trade ? "bg-green-50" : row.will_trade ? "bg-yellow-50" : ""}`}>
                      <td className="px-4 py-3 text-gray-600 font-mono text-xs">{fmt(row.timestamp)}</td>
                      <td className={`px-4 py-3 font-mono ${scoreColor(row.score, row.threshold)}`}>
                        {row.score > 0 ? "+" : ""}{row.score} <span className="text-gray-300">/{row.threshold}</span>
                      </td>
                      <td className="px-4 py-3">{dirBadge(row.direction)}</td>
                      <td className="px-4 py-3">
                        {row.did_trade   ? <span className="px-2 py-0.5 rounded-full text-xs bg-green-200 text-green-800 font-semibold">TRADED</span>
                        : row.will_trade ? <span className="px-2 py-0.5 rounded-full text-xs bg-yellow-200 text-yellow-800 font-semibold">FIRED</span>
                        :                  <span className="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500">SKIP</span>}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-gray-700">{row.nifty_spot ? row.nifty_spot.toLocaleString("en-IN") : "-"}</td>
                      <td className="px-4 py-3 text-right font-mono text-gray-700">
                        {prem ? `₹${prem.toFixed(1)}` : "-"}
                        {row.option_type && row.strike ? <span className="text-xs text-gray-400 ml-1">{row.strike}{row.option_type}</span> : null}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-xs">
                        {pre2 ? <span>₹{pre2.toFixed(1)}<br/><span className={diff2 && diff2 > 0 ? "text-green-600" : "text-red-600"}>{diff2 && diff2 > 0 ? "+" : ""}{diff2?.toFixed(1)}</span></span> : <span className="text-gray-300">-</span>}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-xs">
                        {pre3 ? <span>₹{pre3.toFixed(1)}<br/><span className={diff3 && diff3 > 0 ? "text-green-600" : "text-red-600"}>{diff3 && diff3 > 0 ? "+" : ""}{diff3?.toFixed(1)}</span></span> : <span className="text-gray-300">-</span>}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500 max-w-xs truncate">{row.signals_fired?.replace(/\|/g, " · ") || "-"}</td>
                      <td className="px-4 py-3 text-xs text-gray-400">{row.reason_skipped || "-"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Legend */}
        <div className="mt-3 flex gap-4 text-xs text-gray-400">
          <span><span className="inline-block w-3 h-3 bg-green-100 rounded mr-1"/>TRADED — bot placed a live order</span>
          <span><span className="inline-block w-3 h-3 bg-yellow-100 rounded mr-1"/>FIRED — score crossed threshold but trade was blocked</span>
          <span><span className="inline-block w-3 h-3 bg-gray-100 rounded mr-1"/>SKIP — score below threshold</span>
          <span>-10m / -15m Prem = what the ATM option premium was 2-3 candles before this row (early entry reference)</span>
        </div>
      </div>
    </div>
  );
}
