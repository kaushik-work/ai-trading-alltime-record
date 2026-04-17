"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function authHeaders() {
  const token = localStorage.getItem("aq_token") || "";
  return { Authorization: `Bearer ${token}` };
}

const SOURCE_LABELS: Record<string, string> = {
  fetch_intraday:     "Intraday OHLCV fetch",
  fetch_daily_df:     "Daily OHLCV fetch",
  fetch_intraday_df:  "Intraday DF fetch",
  get_option_ltp:     "Option LTP fetch",
  get_quote:          "Spot price quote",
  live_order_preflight:"Live order preflight",
  live_order_rejected:"Live order rejected",
};

export default function ErrorsPage() {
  const router = useRouter();
  const [errors, setErrors]     = useState<any[]>([]);
  const [loading, setLoading]   = useState(true);
  const [clearing, setClearing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/zerodha-errors`, { headers: authHeaders() });
      if (res.status === 401) { router.push("/login"); return; }
      const data = await res.json();
      setErrors(Array.isArray(data) ? data : []);
      setLastRefresh(new Date());
    } catch {
      setErrors([]);
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [load]);

  async function clearAll() {
    setClearing(true);
    await fetch(`${API_URL}/api/zerodha-errors`, { method: "DELETE", headers: authHeaders() });
    await load();
    setClearing(false);
  }

  // Group by source for the summary
  const sourceCounts: Record<string, number> = {};
  for (const e of errors) {
    sourceCounts[e.source] = (sourceCounts[e.source] || 0) + 1;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-5xl mx-auto px-4 py-8">

        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <button onClick={() => router.push("/")} className="text-xs text-gray-400 hover:text-gray-600 mb-1 block">
              ← Back to dashboard
            </button>
            <h1 className="text-xl font-bold text-gray-900">Angel One Error Log</h1>
            <p className="text-xs text-gray-400 mt-0.5">
              {lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString("en-IN")} · auto every 30s` : "Loading…"}
            </p>
          </div>
          {errors.length > 0 && (
            <button
              onClick={clearAll}
              disabled={clearing}
              className="text-sm px-4 py-2 rounded-lg border font-semibold transition-colors"
              style={{ borderColor: "#ef4444", color: "#ef4444", background: "white" }}
            >
              {clearing ? "Clearing…" : `Clear all (${errors.length})`}
            </button>
          )}
        </div>

        {/* Summary badges */}
        {Object.keys(sourceCounts).length > 0 && (
          <div className="flex flex-wrap gap-2 mb-5">
            {Object.entries(sourceCounts).map(([src, count]) => (
              <span key={src} className="text-xs font-semibold px-3 py-1 rounded-full"
                style={{ background: "#fee2e2", color: "#b91c1c" }}>
                {SOURCE_LABELS[src] ?? src} — {count}
              </span>
            ))}
          </div>
        )}

        {/* Table */}
        {loading ? (
          <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
            Loading…
          </div>
        ) : errors.length === 0 ? (
          <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
            <div className="text-2xl mb-2">✓</div>
            <div className="text-gray-500 font-medium">No errors logged</div>
            <div className="text-xs text-gray-400 mt-1">Angel One is working fine</div>
          </div>
        ) : (
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden overflow-x-auto">
            <table className="w-full text-sm min-w-[600px]">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Time</th>
                  <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Source</th>
                  <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Symbol</th>
                  <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Detail</th>
                  <th className="px-4 py-3 text-left text-xs text-gray-500 font-semibold uppercase">Error</th>
                </tr>
              </thead>
              <tbody>
                {errors.map((e: any, i: number) => (
                  <tr key={i} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 text-xs text-gray-400 whitespace-nowrap">
                      {e.timestamp
                        ? new Date(e.timestamp).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" })
                        : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-xs font-semibold px-2 py-0.5 rounded-full"
                        style={{ background: "#fee2e2", color: "#b91c1c" }}>
                        {SOURCE_LABELS[e.source] ?? e.source}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-semibold text-indigo-600 text-xs">{e.symbol || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-500">{e.detail || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-700 max-w-xs truncate" title={e.error}>{e.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
