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

type BacktestResult = {
  symbol: string;
  trades: number;
  win_rate: number;
  total_pnl: number;
  total_return_pct: number;
  profit_factor: number;
  max_drawdown_pct: number;
};

export default function NseView() {
  const [state, setState] = useState<NseState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [backtestSymbol, setBacktestSymbol] = useState("NIFTY");
  const [backtestCapital, setBacktestCapital] = useState(300000);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestLoading, setBacktestLoading] = useState(false);
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

  async function runBacktest(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBacktestLoading(true);
    setBacktestResult(null);
    try {
      const r = await fetch(`${_API}/api/nse/backtest/synthetic_forward`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          symbol: backtestSymbol,
          source: "mongo",
          capital: backtestCapital,
          interval: 5,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setBacktestResult(data);
    } catch (e: any) {
      setError(e.message || "Backtest failed");
    } finally {
      setBacktestLoading(false);
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
        <div className="p-3 rounded-lg bg-blue-900/30 border border-blue-700 text-blue-200 text-sm space-y-1">
          <p className="font-semibold">Test order placed</p>
          <p>Spot: {testOrderResult.spot} | Strike: {testOrderResult.strike} | Expiry: {new Date(testOrderResult.expiry).toLocaleDateString()}</p>
          <p>Symbol: {testOrderResult.tradingsymbol} | Qty: {testOrderResult.quantity}</p>
          <pre className="text-[11px] text-blue-300 overflow-x-auto">{JSON.stringify(testOrderResult.order_response, null, 2)}</pre>
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

      <div className="border border-[#1e1e30] rounded-lg p-4 bg-[#0e0e1a]">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">Backtest</h2>
        <form onSubmit={runBacktest} className="flex flex-col sm:flex-row gap-3 items-start sm:items-end">
          <div>
            <label className="block text-[11px] text-gray-500 mb-1">Symbol</label>
            <select
              value={backtestSymbol}
              onChange={(e) => setBacktestSymbol(e.target.value)}
              className="bg-[#13131f] border border-[#1e1e30] rounded px-3 py-2 text-sm text-gray-200"
            >
              <option value="NIFTY">NIFTY</option>
              <option value="BANKNIFTY">BANKNIFTY</option>
              <option value="FINNIFTY">FINNIFTY</option>
              <option value="SENSEX">SENSEX</option>
            </select>
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-1">Capital (₹)</label>
            <input
              type="number"
              value={backtestCapital}
              onChange={(e) => setBacktestCapital(Number(e.target.value))}
              className="bg-[#13131f] border border-[#1e1e30] rounded px-3 py-2 text-sm text-gray-200 w-32"
            />
          </div>
          <button
            type="submit"
            disabled={backtestLoading}
            className="px-4 py-2 rounded-lg text-sm font-semibold bg-[#627eea] text-white hover:bg-[#4c66d0] disabled:opacity-50"
          >
            {backtestLoading ? "Running..." : "Run Backtest"}
          </button>
        </form>

        {backtestResult && (
          <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatCard label="Trades" value={String(backtestResult.trades)} />
            <StatCard label="Win Rate" value={`${backtestResult.win_rate.toFixed(1)}%`} />
            <StatCard label="Total PnL" value={`₹${Math.round(backtestResult.total_pnl).toLocaleString()}`} accent={backtestResult.total_pnl >= 0 ? "green" : "red"} />
            <StatCard label="Return" value={`${backtestResult.total_return_pct.toFixed(2)}%`} accent={backtestResult.total_return_pct >= 0 ? "green" : "red"} />
            <StatCard label="Profit Factor" value={backtestResult.profit_factor.toFixed(2)} />
            <StatCard label="Max DD" value={`${backtestResult.max_drawdown_pct.toFixed(2)}%`} accent="red" />
          </div>
        )}
      </div>

      <p className="text-xs text-gray-500">
        NSE strategy: synthetic-forward combo on index options. Live margin is fetched from Angel One
        before every order. Shared capital pool = ₹{state?.total_capital.toLocaleString() ?? "—"}.
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
