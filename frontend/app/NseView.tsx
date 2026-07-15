"use client";
import { useEffect, useState } from "react";

const _API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type NseLeg = {
  side: string;
  type: string;
  strike: number;
  lots: number;
  filled_px: number | null;
};

type NsePosition = {
  position_id: string;
  symbol: string;
  side: string;
  entry_time: string;
  pred_pct: number;
  spot_at_entry: number;
  max_hold_until: string;
  legs: NseLeg[];
};

type NseState = {
  enabled: boolean;
  mode: string;
  killed: boolean;
  day_pnl: number;
  total_capital: number;
  margin_used: number;
  margin_available: number;
  open_positions: NsePosition[];
  journal_count: number;
};

export default function NseView() {
  const [state, setState] = useState<NseState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [testOrderLoading, setTestOrderLoading] = useState(false);
  const [testOrderResult, setTestOrderResult] = useState<any>(null);

  const token = typeof window !== "undefined" ? localStorage.getItem("aq_token") : null;

  async function fetchStatus() {
    if (!token) return;
    try {
      const r = await fetch(`${_API}/api/nse/status`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setState(data);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Failed to fetch NSE status");
    }
  }

  async function toggleKill() {
    if (!token || !state) return;
    setKillBusy(true);
    try {
      const endpoint = state.killed ? "/api/nse/unkill" : "/api/nse/kill";
      const r = await fetch(`${_API}${endpoint}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await fetchStatus();
    } catch (e: any) {
      setError(e.message || "Kill switch failed");
    } finally {
      setKillBusy(false);
    }
  }

  async function placeTestBuyCe() {
    if (!token) return;
    if (!confirm("This will place a REAL buy CE market order for 1 lot of NIFTY. Continue?")) return;
    setTestOrderLoading(true);
    setTestOrderResult(null);
    try {
      const r = await fetch(`${_API}/api/nse/test_buy_ce`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      setTestOrderResult(data);
    } catch (e: any) {
      setError(e.message || "Test order failed");
    } finally {
      setTestOrderLoading(false);
    }
  }

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 5000);
    return () => clearInterval(id);
  }, [token]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold text-[#16a34a]">NSE · Synthetic Forward</h1>
          <p className="text-xs text-gray-500 mt-1">
            Angel One SmartAPI · NIFTY / BANKNIFTY / FINNIFTY / SENSEX
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={placeTestBuyCe}
            disabled={testOrderLoading}
            className="px-4 py-2 rounded-lg text-sm font-semibold border border-blue-500 text-blue-400 bg-blue-600/20 hover:bg-blue-600/30 transition-colors"
          >
            {testOrderLoading ? "Placing..." : "TEST Buy CE"}
          </button>
          <button
            onClick={toggleKill}
            disabled={killBusy || !state}
            className={`px-4 py-2 rounded-lg text-sm font-semibold border transition-colors ${
              state?.killed
                ? "bg-red-600/20 border-red-500 text-red-400 hover:bg-red-600/30"
                : "bg-green-600/20 border-green-500 text-green-400 hover:bg-green-600/30"
            }`}
          >
            {killBusy ? "Working..." : state?.killed ? "RESUME NSE" : "KILL NSE"}
          </button>
        </div>
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-red-900/30 border border-red-700 text-red-200 text-sm">
          {error}
        </div>
      )}

      {testOrderResult && (
        <div className={`p-3 rounded-lg border text-sm space-y-1 ${
          testOrderResult.order_response?.status === true
            ? "bg-green-900/30 border-green-700 text-green-200"
            : "bg-red-900/30 border-red-700 text-red-200"
        }`}>
          <p className="font-semibold">
            {testOrderResult.order_response?.status === true
              ? `Order accepted — ID: ${testOrderResult.order_response.order_id}`
              : "Order rejected / failed"}
          </p>
          <p>Spot: {testOrderResult.spot} | Strike: {testOrderResult.strike} | Expiry: {new Date(testOrderResult.expiry).toLocaleDateString()}</p>
          <p>Symbol: {testOrderResult.tradingsymbol} | Qty: {testOrderResult.quantity}</p>
          <p>Available cash: ₹{Number(testOrderResult.available_cash || 0).toLocaleString()}</p>
          <pre className="text-[11px] overflow-x-auto">{JSON.stringify(testOrderResult.order_response, null, 2)}</pre>
        </div>
      )}

      {state && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 sm:gap-4">
            <StatCard label="Runner" value={state.enabled ? "ON" : "OFF"} accent={state.enabled ? "green" : "gray"} />
            <StatCard label="Day PnL" value={`₹${state.day_pnl.toLocaleString()}`} accent={state.day_pnl >= 0 ? "green" : "red"} />
            <StatCard label="Margin Used" value={`₹${Math.round(state.margin_used).toLocaleString()}`} />
            <StatCard label="Available" value={`₹${Math.round(state.margin_available).toLocaleString()}`} accent="green" />
          </div>

          <div className="border border-[#1e1e30] rounded-lg p-4 bg-[#0e0e1a]">
            <h2 className="text-sm font-semibold text-gray-300 mb-3">Open Positions ({state.open_positions.length})</h2>
            {state.open_positions.length === 0 ? (
              <p className="text-sm text-gray-500">No open NSE positions.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs sm:text-sm min-w-[560px]">
                  <thead className="text-gray-500 border-b border-[#1e1e30]">
                    <tr>
                      <th className="text-left py-2 px-2">Symbol</th>
                      <th className="text-right px-2">Side</th>
                      <th className="text-right px-2">Entry</th>
                      <th className="text-right px-2">Spot @ Entry</th>
                      <th className="text-right px-2">Pred %</th>
                      <th className="text-right px-2">Legs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {state.open_positions.map((p) => (
                      <tr key={p.position_id} className="border-b border-[#13131f]">
                        <td className="py-2 px-2">{p.symbol}</td>
                        <td className={`text-right px-2 ${p.side === "long" ? "text-green-400" : "text-red-400"}`}>
                          {p.side.toUpperCase()}
                        </td>
                        <td className="text-right px-2">{new Date(p.entry_time).toLocaleString()}</td>
                        <td className="text-right px-2">{p.spot_at_entry.toLocaleString()}</td>
                        <td className="text-right px-2">{(p.pred_pct).toFixed(3)}%</td>
                        <td className="text-right px-2 text-gray-400">
                          {p.legs.map((l) => `${l.side} ${l.strike}${l.type}`).join(" / ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}

      <p className="text-xs text-gray-500">
        Live NSE trading via Angel One SmartAPI. Shared capital pool = ₹{state?.total_capital.toLocaleString() ?? "—"}.
      </p>
    </div>
  );
}

function StatCard({ label, value, accent }: { label: string; value: string; accent?: "green" | "red" | "gray" }) {
  const color = accent === "green" ? "text-green-400" : accent === "red" ? "text-red-400" : accent === "gray" ? "text-gray-400" : "text-white";
  return (
    <div className="border border-[#1e1e30] rounded-lg px-3 sm:px-4 py-2.5 sm:py-3 bg-[#0e0e1a] min-w-0">
      <p className="text-[11px] sm:text-xs text-gray-500 truncate">{label}</p>
      <p className={`text-base sm:text-lg font-semibold ${color} mt-1 truncate`}>{value}</p>
    </div>
  );
}
