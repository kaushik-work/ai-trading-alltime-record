"use client";
import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface MongoStatus {
  enabled: boolean;
  db_name?: string | null;
  url_host?: string | null;
  counts?: Record<string, number | string>;
  latest_trade_ts?: string | null;
  latest_snapshot_ts?: string | null;
  error?: string;
  checked_at?: string;
}

/**
 * Compact Mongo mirror health chip — green when connected, red otherwise.
 * Hover the chip to see db name, host, and per-collection counts.
 *
 * Polls /api/mongo/status every 60s. The endpoint itself caches 30s so this
 * is cheap. Only renders if the API responds — silent on network errors so
 * a backend hiccup doesn't make the chip flash red.
 */
export default function MongoBadge() {
  const [status, setStatus] = useState<MongoStatus | null>(null);
  const [hover,  setHover]  = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function fetchStatus() {
      const token = localStorage.getItem("aq_token");
      if (!token) return;
      try {
        const r = await fetch(`${API_URL}/api/mongo/status`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!r.ok) return;
        const j = await r.json();
        if (!cancelled) setStatus(j);
      } catch { /* network blip — keep last status */ }
    }
    fetchStatus();
    const t = setInterval(fetchStatus, 60_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  if (!status) return null;
  const ok = !!status.enabled;

  return (
    <span
      className="relative flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full cursor-help"
      style={ok
        ? { background: "#f0fdf4", color: "#15803d" }
        : { background: "#fee2e2", color: "#dc2626" }
      }
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <span
        className="w-1.5 h-1.5 rounded-full inline-block"
        style={{ background: ok ? "#22c55e" : "#ef4444" }}
      />
      {ok ? "MONGO" : "MONGO ✗"}

      {hover && (
        <div className="absolute left-0 top-full mt-1 z-50 w-72 bg-white border border-gray-200 rounded-lg shadow-xl p-3 text-left text-[11px] text-gray-700 normal-case">
          <div className="font-bold text-gray-900 mb-1">
            Mongo mirror {ok ? "✓ connected" : "✗ disconnected"}
          </div>
          {status.db_name && (
            <div className="text-gray-500">
              db: <span className="font-mono">{status.db_name}</span>
            </div>
          )}
          {status.url_host && (
            <div className="text-gray-500 truncate">
              host: <span className="font-mono">{status.url_host}</span>
            </div>
          )}
          {ok && status.counts && (
            <div className="mt-2 grid grid-cols-2 gap-x-2 gap-y-0.5">
              {Object.entries(status.counts).map(([k, v]) => (
                <div key={k} className="flex justify-between">
                  <span className="text-gray-500">{k}</span>
                  <span className="font-bold text-gray-800">
                    {typeof v === "number" ? v.toLocaleString("en-IN") : String(v)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {ok && (status.latest_trade_ts || status.latest_snapshot_ts) && (
            <div className="mt-2 text-gray-400 text-[10px]">
              {status.latest_trade_ts && (
                <div>last trade: {String(status.latest_trade_ts).slice(0, 19)}</div>
              )}
              {status.latest_snapshot_ts && (
                <div>last snapshot: {String(status.latest_snapshot_ts).slice(0, 19)}</div>
              )}
            </div>
          )}
          {!ok && status.error && (
            <div className="mt-1 text-red-600">{status.error}</div>
          )}
        </div>
      )}
    </span>
  );
}
