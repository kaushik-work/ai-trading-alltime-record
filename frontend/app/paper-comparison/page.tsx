"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function authHeaders() {
  const token = localStorage.getItem("aq_token") || "";
  return { Authorization: `Bearer ${token}` };
}

function pnlColor(pnl: number) {
  if (pnl > 0) return "#16a34a";
  if (pnl < 0) return "#dc2626";
  return "#6b7280";
}

function pnlBg(pnl: number) {
  if (pnl > 0) return "#f0fdf4";
  if (pnl < 0) return "#fef2f2";
  return "#f9fafb";
}

function fmt(pnl: number) {
  const sign = pnl >= 0 ? "+" : "";
  return `${sign}₹${Math.abs(pnl).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" });
}

function WinnerBadge({ winner }: { winner: string }) {
  const color = winner === "BUYER" ? "#2563eb" : "#7c3aed";
  const bg    = winner === "BUYER" ? "#eff6ff" : "#f5f3ff";
  return (
    <span style={{ background: bg, color, padding: "2px 10px", borderRadius: 99, fontWeight: 700, fontSize: 12 }}>
      {winner} ahead
    </span>
  );
}

export default function PaperComparisonPage() {
  const router = useRouter();
  const [data, setData]         = useState<any>(null);
  const [loading, setLoading]   = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/paper-comparison`, { headers: authHeaders() });
      if (res.status === 401) { router.push("/login"); return; }
      const json = await res.json();
      setData(json);
      setLastRefresh(new Date());
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    load();
    const iv = setInterval(load, 30_000);
    return () => clearInterval(iv);
  }, [load]);

  const summary  = data?.summary  ?? {};
  const trades   = data?.trades   ?? [];
  const open     = data?.open_positions ?? [];
  const noTrades = !loading && trades.length === 0 && open.length === 0;

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-6xl mx-auto px-4 py-8">

        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <button onClick={() => router.push("/")} className="text-xs text-gray-400 hover:text-gray-600 mb-1 block">
              ← Back to dashboard
            </button>
            <h1 className="text-xl font-bold text-gray-900">Paper Comparison — Buyer vs Seller</h1>
            <p className="text-xs text-gray-400 mt-0.5">
              {lastRefresh
                ? `Refreshed ${lastRefresh.toLocaleTimeString("en-IN")} · auto every 30s`
                : "Loading…"}
            </p>
          </div>
          <div className="text-xs text-gray-400 text-right">
            Paper only · No real orders placed<br/>
            Same signal, opposite option side
          </div>
        </div>

        {/* Summary cards */}
        {!loading && summary.total_trades > 0 && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <SummaryCard
              label="Buyer Total P&L"
              value={fmt(summary.buyer_total_pnl ?? 0)}
              sub={`${summary.buyer_wins ?? 0}W / ${summary.buyer_losses ?? 0}L`}
              pnl={summary.buyer_total_pnl ?? 0}
              color="#2563eb"
            />
            <SummaryCard
              label="Seller Total P&L"
              value={fmt(summary.seller_total_pnl ?? 0)}
              sub={`${summary.seller_wins ?? 0}W / ${summary.seller_losses ?? 0}L`}
              pnl={summary.seller_total_pnl ?? 0}
              color="#7c3aed"
            />
            <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-col justify-between">
              <div className="text-xs text-gray-400 font-semibold uppercase tracking-wide mb-1">Trades Compared</div>
              <div className="text-2xl font-bold text-gray-800">{summary.total_trades}</div>
              <div className="text-xs text-gray-400 mt-1">{open.length} open now</div>
            </div>
            <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-col justify-between">
              <div className="text-xs text-gray-400 font-semibold uppercase tracking-wide mb-1">Leading</div>
              <div className="mt-1">
                {summary.winner ? <WinnerBadge winner={summary.winner} /> : <span className="text-gray-400 text-sm">—</span>}
              </div>
              <div className="text-xs text-gray-400 mt-2">
                Δ {summary.winner
                  ? fmt(Math.abs((summary.buyer_total_pnl ?? 0) - (summary.seller_total_pnl ?? 0)))
                  : "—"}
              </div>
            </div>
          </div>
        )}

        {/* Open positions */}
        {open.length > 0 && (
          <div className="mb-6">
            <h2 className="text-sm font-bold text-gray-600 uppercase tracking-wide mb-3">Live / Open Positions</h2>
            <div className="space-y-3">
              {open.map((pos: any) => (
                <TradeRow key={pos.id} pos={pos} isOpen />
              ))}
            </div>
          </div>
        )}

        {/* Closed trades */}
        {noTrades ? (
          <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
            <div className="text-3xl mb-2">📊</div>
            <div className="text-gray-600 font-semibold">No paper trades yet</div>
            <div className="text-xs text-gray-400 mt-1 max-w-xs mx-auto">
              Paper trades open automatically when any strategy fires a will_trade signal during market hours.
            </div>
          </div>
        ) : trades.length > 0 ? (
          <div>
            <h2 className="text-sm font-bold text-gray-600 uppercase tracking-wide mb-3">Closed Trades</h2>
            <div className="space-y-3">
              {[...trades].reverse().map((pos: any) => (
                <TradeRow key={pos.id} pos={pos} isOpen={false} />
              ))}
            </div>
          </div>
        ) : null}

      </div>
    </div>
  );
}

function SummaryCard({ label, value, sub, pnl, color }: {
  label: string; value: string; sub: string; pnl: number; color: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-col justify-between"
         style={{ borderLeft: `4px solid ${color}` }}>
      <div className="text-xs text-gray-400 font-semibold uppercase tracking-wide mb-1">{label}</div>
      <div className="text-2xl font-bold" style={{ color: pnlColor(pnl) }}>{value}</div>
      <div className="text-xs text-gray-400 mt-1">{sub}</div>
    </div>
  );
}

function TradeRow({ pos, isOpen }: { pos: any; isOpen: boolean }) {
  const buyer  = pos.buyer  ?? {};
  const seller = pos.seller ?? {};
  const buyerPnl  = buyer.pnl  ?? 0;
  const sellerPnl = seller.pnl ?? 0;

  const dirColor = pos.direction === "BUY" ? "#16a34a" : "#dc2626";

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100">
        <span className="text-xs font-bold px-2 py-0.5 rounded"
              style={{ background: "#f1f5f9", color: "#334155" }}>
          {pos.strategy}
        </span>
        <span className="text-xs font-semibold px-2 py-0.5 rounded-full"
              style={{ background: pos.direction === "BUY" ? "#dcfce7" : "#fee2e2", color: dirColor }}>
          {pos.direction}
        </span>
        <span className="text-xs text-gray-400">Score: {pos.score}</span>
        <span className="text-xs text-gray-400">Strike: {pos.strike}</span>
        <span className="text-xs text-gray-400">Expiry: {pos.expiry}</span>
        <span className="text-xs text-gray-400">NIFTY @{pos.nifty_entry?.toFixed(0)}</span>
        {isOpen && (
          <span className="ml-auto text-xs font-bold px-2 py-0.5 rounded-full"
                style={{ background: "#fef9c3", color: "#92400e" }}>OPEN</span>
        )}
        <span className="text-xs text-gray-400 ml-auto">{fmtTime(pos.entry_time)}</span>
      </div>

      {/* Buyer vs Seller comparison */}
      <div className="grid grid-cols-2 divide-x divide-gray-100">
        {/* Buyer */}
        <div className="px-4 py-3" style={{ background: pnlBg(buyerPnl) }}>
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-bold text-blue-700 uppercase tracking-wide">Buyer</span>
            <span className="text-xs text-gray-400">
              {buyer.option_type} · Entry ₹{buyer.entry_premium?.toFixed(0)}
              {buyer.current_premium !== buyer.entry_premium
                ? ` → ₹${buyer.current_premium?.toFixed(0)}`
                : ""}
            </span>
          </div>
          <div className="text-xl font-bold" style={{ color: pnlColor(buyerPnl) }}>
            {fmt(buyerPnl)}
          </div>
          <div className="flex gap-3 mt-1 text-xs text-gray-400">
            <span>SL ₹{buyer.sl?.toFixed(0)}</span>
            <span>TP ₹{buyer.tp?.toFixed(0)}</span>
            {buyer.exit_reason && (
              <span className="font-semibold" style={{ color: buyer.exit_reason === "TP" ? "#16a34a" : buyer.exit_reason === "SL" ? "#dc2626" : "#6b7280" }}>
                {buyer.exit_reason} @ ₹{buyer.exit_premium?.toFixed(0)}
              </span>
            )}
          </div>
        </div>

        {/* Seller */}
        <div className="px-4 py-3" style={{ background: pnlBg(sellerPnl) }}>
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-bold text-purple-700 uppercase tracking-wide">Seller</span>
            <span className="text-xs text-gray-400">
              SELL {seller.option_type} · Collected ₹{seller.entry_premium?.toFixed(0)}
              {seller.current_premium !== seller.entry_premium
                ? ` → ₹${seller.current_premium?.toFixed(0)}`
                : ""}
            </span>
          </div>
          <div className="text-xl font-bold" style={{ color: pnlColor(sellerPnl) }}>
            {fmt(sellerPnl)}
          </div>
          <div className="flex gap-3 mt-1 text-xs text-gray-400">
            <span>SL ₹{seller.sl?.toFixed(0)}</span>
            <span>TP ₹{seller.tp?.toFixed(0)}</span>
            {seller.exit_reason && (
              <span className="font-semibold" style={{ color: seller.exit_reason === "TP" ? "#16a34a" : seller.exit_reason === "SL" ? "#dc2626" : "#6b7280" }}>
                {seller.exit_reason} @ ₹{seller.exit_premium?.toFixed(0)}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Winner of this trade */}
      {!isOpen && (
        <div className="px-4 py-2 bg-gray-50 border-t border-gray-100 flex items-center gap-3">
          <span className="text-xs text-gray-400">Winner:</span>
          {buyerPnl === sellerPnl ? (
            <span className="text-xs font-semibold text-gray-500">Tie</span>
          ) : buyerPnl > sellerPnl ? (
            <span className="text-xs font-bold text-blue-700">Buyer +{fmt(buyerPnl - sellerPnl)} advantage</span>
          ) : (
            <span className="text-xs font-bold text-purple-700">Seller +{fmt(sellerPnl - buyerPnl)} advantage</span>
          )}
          <span className="ml-auto text-xs text-gray-400">{fmtTime(pos.exit_time)}</span>
        </div>
      )}
    </div>
  );
}
