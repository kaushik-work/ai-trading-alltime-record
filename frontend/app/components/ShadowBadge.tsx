"use client";
import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface ShadowTrade {
  signal_id: string;
  strategy?: string;
  date: string;
  side: string;
  strike: number;
  entry_dt: string;
  entry_premium: number;
  sl_price: number;
  tp_price: number;
  threshold: number;
  spot_at_entry: number;
  status: "OPEN" | "CLOSED";
  exit_dt?: string;
  exit_premium?: number;
  reason?: string;
  pnl?: number;
}

interface StrategyBlock {
  open_position: ShadowTrade | null;
  today_pnl: number;
  total_pnl: number;
  closed_count: number;
  wins_count: number;
  win_rate: number;
  trades: ShadowTrade[];
}

interface ShadowStatus {
  enabled: boolean;
  strategies: Record<string, StrategyBlock>;
  aggregate: {
    today_pnl: number;
    total_pnl: number;
    open_count: number;
    strategy_count: number;
  } | null;
}

const STRATEGY_LABELS: Record<string, string> = {
  q5_straddle_level: "STRADDLE",
  q5_straddle_mom3:  "STR-MOM",
  q5_pcr_mom3:       "PCR-MOM",
};

/**
 * Multi-strategy SHADOW signal badge.
 *
 * Shows one mini-chip per strategy (STRADDLE / STR-MOM / PCR-MOM).
 * Each chip color:
 *   • blue dot      — that strategy has an open shadow trade
 *   • green chip    — today's P&L > 0
 *   • red chip      — today's P&L < 0
 *   • gray chip     — idle
 *
 * Hover the aggregate chip to see per-strategy detail.
 * Shadow trades are SIMULATED — no real orders are ever placed.
 */
export default function ShadowBadge() {
  const [s, setS] = useState<ShadowStatus | null>(null);
  const [hover, setHover] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function fetchStatus() {
      const token = localStorage.getItem("aq_token");
      if (!token) return;
      try {
        const r = await fetch(`${API_URL}/api/shadow-trades`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!r.ok) return;
        const j = await r.json();
        if (!cancelled) setS(j);
      } catch { /* network blip */ }
    }
    fetchStatus();
    const t = setInterval(fetchStatus, 60_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  if (!s || !s.enabled || !s.aggregate) return null;

  const agg = s.aggregate;
  const aggPnl = agg.today_pnl;
  const aggColor =
    agg.open_count > 0 ? { bg: "#dbeafe", fg: "#1d4ed8", dot: "#3b82f6" } :
    aggPnl > 0         ? { bg: "#dcfce7", fg: "#15803d", dot: "#22c55e" } :
    aggPnl < 0         ? { bg: "#fee2e2", fg: "#dc2626", dot: "#ef4444" } :
                         { bg: "#f3f4f6", fg: "#6b7280", dot: "#9ca3af" };

  const aggLabel =
    agg.open_count > 0
      ? `SHADOW ${agg.open_count}/${agg.strategy_count} OPEN`
      : aggPnl !== 0
        ? `SHADOW ₹${aggPnl > 0 ? "+" : ""}${Math.round(aggPnl).toLocaleString("en-IN")}`
        : `SHADOW ×${agg.strategy_count}`;

  return (
    <span
      className="relative flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full cursor-help"
      style={{ background: aggColor.bg, color: aggColor.fg }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <span className="w-1.5 h-1.5 rounded-full inline-block"
            style={{ background: aggColor.dot }} />
      {aggLabel}

      {hover && (
        <div className="absolute left-0 top-full mt-1 z-50 w-96 bg-white border border-gray-200 rounded-lg shadow-xl p-3 text-left text-[11px] text-gray-700 normal-case">
          <div className="font-bold text-gray-900 mb-1">
            Multi-strategy shadow signal
          </div>
          <div className="text-gray-500 mb-3">
            Forward-test ledger across {agg.strategy_count} independent signals. No real orders.
          </div>

          {Object.entries(s.strategies).map(([name, b]) => {
            const label = STRATEGY_LABELS[name] || name;
            const open = b.open_position;
            const todayPnl = b.today_pnl;
            return (
              <div key={name} className="mb-3 border-b border-gray-100 pb-2 last:border-b-0">
                <div className="flex justify-between mb-1">
                  <span className="font-bold text-gray-800">{label}</span>
                  <span className="text-gray-500 text-[10px]">{name}</span>
                </div>
                <div className="grid grid-cols-3 gap-x-2 gap-y-0.5">
                  <div className="flex justify-between col-span-3">
                    <span className="text-gray-500">today</span>
                    <span className={`font-bold ${todayPnl >= 0 ? "text-green-700" : "text-red-700"}`}>
                      ₹{todayPnl > 0 ? "+" : ""}{Math.round(todayPnl).toLocaleString("en-IN")}
                    </span>
                  </div>
                  <div className="flex justify-between col-span-3">
                    <span className="text-gray-500">total ({b.closed_count} closed)</span>
                    <span className={`font-bold ${b.total_pnl >= 0 ? "text-green-700" : "text-red-700"}`}>
                      ₹{b.total_pnl > 0 ? "+" : ""}{Math.round(b.total_pnl).toLocaleString("en-IN")}
                    </span>
                  </div>
                  <div className="flex justify-between col-span-3">
                    <span className="text-gray-500">win rate</span>
                    <span className="font-bold text-gray-800">{b.win_rate}%</span>
                  </div>
                </div>
                {open && (
                  <div className="mt-1 p-1.5 bg-blue-50 rounded text-[10px]">
                    OPEN {open.strike}{open.side} @ ₹{open.entry_premium.toFixed(2)} ·
                    SL ₹{open.sl_price.toFixed(2)} · TP ₹{open.tp_price.toFixed(2)}
                  </div>
                )}
              </div>
            );
          })}

          <div className="mt-2 pt-2 border-t border-gray-200 grid grid-cols-2 gap-x-2 gap-y-0.5">
            <div className="flex justify-between col-span-2">
              <span className="text-gray-700 font-bold">aggregate today</span>
              <span className={`font-bold ${agg.today_pnl >= 0 ? "text-green-700" : "text-red-700"}`}>
                ₹{agg.today_pnl > 0 ? "+" : ""}{Math.round(agg.today_pnl).toLocaleString("en-IN")}
              </span>
            </div>
            <div className="flex justify-between col-span-2">
              <span className="text-gray-700 font-bold">aggregate total</span>
              <span className={`font-bold ${agg.total_pnl >= 0 ? "text-green-700" : "text-red-700"}`}>
                ₹{agg.total_pnl > 0 ? "+" : ""}{Math.round(agg.total_pnl).toLocaleString("en-IN")}
              </span>
            </div>
          </div>
        </div>
      )}
    </span>
  );
}
