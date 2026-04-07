"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import Header from "../components/Header";
import { useWebSocket } from "../hooks/useWebSocket";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL  = API_URL.replace(/^http/, "ws") + "/ws";

function authHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${localStorage.getItem("aq_token") ?? ""}`,
  };
}

function KpiCard({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) {
  const colorMap: Record<string, string> = {
    "text-green-600": "#16a34a",
    "text-red-500":   "#dc2626",
    "text-gray-900":  "#111827",
  };
  const textColor = color ? (colorMap[color] ?? "#111827") : "#111827";
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
      <div className="text-xs uppercase tracking-widest font-semibold mb-1" style={{ color: "#9ca3af" }}>{label}</div>
      <div className="text-xl font-bold" style={{ color: textColor }}>{value}</div>
      {sub && <div className="text-xs mt-0.5" style={{ color: "#9ca3af" }}>{sub}</div>}
    </div>
  );
}

export default function BacktestPage() {
  const router = useRouter();
  useEffect(() => {
    if (!localStorage.getItem("aq_token")) router.push("/login");
  }, []);

  const { data: wsData, connected } = useWebSocket(WS_URL);

  const [strategy,  setStrategy]  = useState("ATR Intraday");
  const [symbol,    setSymbol]    = useState("NIFTY");
  const [interval,  setInterval]  = useState("5m");
  const [period,    setPeriod]    = useState("60d");
  const [capital,   setCapital]   = useState(125000);
  const [minScore,  setMinScore]  = useState(6);
  const [riskPct,   setRiskPct]   = useState(2.0);
  const [dailyLoss, setDailyLoss] = useState(5.0);
  const [rrRatio,   setRrRatio]   = useState(2.5);

  // Trade log filters
  const [filterExit,   setFilterExit]   = useState("All");
  const [filterResult, setFilterResult] = useState("All");
  const [filterSearch, setFilterSearch] = useState("");

  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState("");
  const [metrics,  setMetrics]  = useState<any>(null);
  const [result,   setResult]   = useState<any>(null);

  async function runBacktest() {
    setLoading(true);
    setError("");
    setMetrics(null);
    setResult(null);
    try {
      const res = await fetch(`${API_URL}/api/backtest`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ strategy, symbol, interval, period, capital, min_score: minScore, risk_pct: riskPct, daily_loss_limit_pct: dailyLoss, rr_ratio: rrRatio }),
      });
      if (!res.ok) { setError((await res.json()).detail ?? "Backtest failed"); return; }
      const data = await res.json();
      setMetrics(data.metrics);
      setResult(data.result);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const pnlColor = metrics ? (metrics.total_pnl >= 0 ? "text-green-600" : "text-red-500") : undefined;

  return (
    <div className="min-h-screen bg-[#f0f2f5]">
      <Header mode={wsData?.mode ?? "paper"} connected={connected}
              botStatus={wsData?.bot_status ?? "unknown"} onBotToggle={() => {}} />

      <div className="max-w-7xl mx-auto p-6 space-y-5">

        {/* Page title */}
        <div className="flex items-center gap-3">
          <button onClick={() => router.push("/")} className="text-gray-400 hover:text-gray-700 text-sm font-medium">← Back</button>
          <div className="w-px h-4 bg-gray-200" />
          <span className="font-bold text-gray-800 text-sm">Strategy Backtester</span>
        </div>

        {/* ── Controls ── */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 mb-4">
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">Strategy</label>
              <select value={strategy} onChange={e => {
                const s = e.target.value;
                setStrategy(s);
                setInterval(s === "Fib-OF" ? "15m" : "5m");
                setMinScore(s === "C-ICT" ? 2 : 6);
                setRrRatio(s === "Fib-OF" ? 3.0 : 2.5);
              }} className="aq-input">
                <option>ATR Intraday</option>
                <option>C-ICT</option>
                <option>Fib-OF</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">Symbol</label>
              <select value={symbol} onChange={e => setSymbol(e.target.value)} className="aq-input">
                <option>NIFTY</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">Timeframe</label>
              <select value={interval} onChange={e => setInterval(e.target.value)} className="aq-input">
                <option value="5m">5 min</option>
                <option value="15m">15 min</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">Period</label>
              <select value={period} onChange={e => setPeriod(e.target.value)} className="aq-input">
                <option value="30d">30 Days</option>
                <option value="60d">60 Days</option>
                <option value="90d">90 Days</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">Capital (₹)</label>
              <input type="number" value={capital} onChange={e => setCapital(Number(e.target.value))}
                     step={5000} min={5000} className="aq-input" />
            </div>
          </div>

          <div className="grid grid-cols-4 gap-4 mb-5">
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">
                Min Signal Score: <span className="text-indigo-600">{minScore}</span>
              </label>
              <input type="range" min={strategy === "C-ICT" ? 1 : 4} max={strategy === "C-ICT" ? 4 : 9}
                     value={minScore} onChange={e => setMinScore(Number(e.target.value))}
                     className="w-full accent-indigo-600" />
              <div className="flex justify-between text-xs text-gray-400 mt-0.5">
                <span>{strategy === "C-ICT" ? 1 : 4}</span><span>{strategy === "C-ICT" ? 4 : 9}</span>
              </div>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">
                Risk per Trade: <span className="text-indigo-600">{riskPct}%</span>
              </label>
              <input type="range" min={1} max={5} step={0.5} value={riskPct} onChange={e => setRiskPct(Number(e.target.value))}
                     className="w-full accent-indigo-600" />
              <div className="flex justify-between text-xs text-gray-400 mt-0.5"><span>1%</span><span>5%</span></div>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">
                Daily Loss Limit: <span className="text-indigo-600">{dailyLoss}%</span>
              </label>
              <input type="range" min={1} max={5} step={0.5} value={dailyLoss} onChange={e => setDailyLoss(Number(e.target.value))}
                     className="w-full accent-indigo-600" />
              <div className="flex justify-between text-xs text-gray-400 mt-0.5"><span>1%</span><span>5%</span></div>
            </div>
            <div>
              <label className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1.5 block">
                R:R Ratio: <span className="text-indigo-600">1:{rrRatio}</span>
              </label>
              <input type="range" min={2} max={5} step={0.5} value={rrRatio} onChange={e => setRrRatio(Number(e.target.value))}
                     className="w-full accent-indigo-600" />
              <div className="flex justify-between text-xs text-gray-400 mt-0.5"><span>1:2</span><span>1:5</span></div>
            </div>
          </div>

          <button onClick={runBacktest} disabled={loading}
            className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-semibold text-sm rounded-lg transition-colors flex items-center gap-2">
            {loading ? (
              <><span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" /> Running...</>
            ) : "▶ Run Backtest"}
          </button>
          {error && <p className="text-red-500 text-sm mt-3">{error}</p>}
        </div>

        {/* ── Results ── */}
        {metrics && !metrics.error && result && (
          <>
            {/* KPI rows */}
            <div className="grid grid-cols-5 gap-3">
              <KpiCard label="Net P&L"      value={`₹${metrics.total_pnl?.toLocaleString("en-IN")}`} color={pnlColor} />
              <KpiCard label="Gross P&L"    value={`₹${metrics.total_pnl_gross?.toLocaleString("en-IN")}`} color={pnlColor} />
              <KpiCard label="Total Charges" value={`₹${metrics.total_charges?.toLocaleString("en-IN")}`} color="text-red-500"
                        sub="Brokerage+STT+Exchange+GST" />
              <KpiCard label="Total Trades" value={metrics.total_trades} sub={`${metrics.win_trades}W / ${metrics.loss_trades}L`} />
              <KpiCard label="Max Drawdown" value={`${metrics.max_drawdown_pct}%`} color="text-red-500" />
            </div>
            <div className="grid grid-cols-5 gap-3">
              <KpiCard label="Win Rate"      value={`${metrics.win_rate}%`} />
              <KpiCard label="Avg Win"       value={`₹${metrics.avg_win?.toLocaleString("en-IN")}`} color="text-green-600" />
              <KpiCard label="Avg Loss"      value={`₹${metrics.avg_loss?.toLocaleString("en-IN")}`} color="text-red-500" />
              <KpiCard label="Profit Factor" value={metrics.profit_factor} />
              <KpiCard label="Sharpe Ratio"  value={metrics.sharpe_ratio} />
            </div>

            {/* Equity curve + Exit breakdown */}
            <div className="grid grid-cols-4 gap-4">
              <div className="col-span-3 bg-white rounded-xl border border-gray-200 shadow-sm p-5">
                <div className="text-sm font-bold text-gray-800 mb-1">Equity Curve</div>
                <div className="text-xs text-gray-400 mb-4">
                  ₹{result.initial_capital?.toLocaleString("en-IN")} → ₹{result.final_equity?.toLocaleString("en-IN")}
                </div>
                {result.equity_curve?.length > 0 ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={result.equity_curve}>
                      <XAxis dataKey="date" hide />
                      <YAxis tickFormatter={(v) => `₹${(v/1000).toFixed(0)}k`}
                             tick={{ fill: "#9ca3af", fontSize: 11 }} width={55} />
                      <Tooltip
                        contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
                        formatter={(v: any) => [`₹${Number(v).toLocaleString("en-IN")}`, "Equity"]}
                      />
                      <ReferenceLine y={result.initial_capital} stroke="#e5e7eb" strokeDasharray="4 4" />
                      <Line type="monotone" dataKey="equity" stroke="#6366f1"
                            strokeWidth={2} dot={false} activeDot={{ r: 4 }} />
                    </LineChart>
                  </ResponsiveContainer>
                ) : <p className="text-gray-400 text-sm text-center py-10">No equity data</p>}
              </div>

              <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5 space-y-4">
                <div>
                  <div className="text-sm font-bold text-gray-800 mb-3">Exit Types</div>
                  {Object.entries(metrics.exit_breakdown ?? {}).map(([k, v]: any) => (
                    <div key={k} className="flex justify-between text-sm py-1.5 border-b border-gray-50 last:border-0">
                      <span className="text-gray-500">{k}</span>
                      <span className="font-semibold text-gray-800">{v}</span>
                    </div>
                  ))}
                </div>
                <div>
                  <div className="text-sm font-bold text-gray-800 mb-3">Streaks</div>
                  <div className="flex justify-between text-sm py-1.5 border-b border-gray-50">
                    <span style={{ color: "#6b7280" }}>Best Win Streak</span>
                    <span className="font-semibold" style={{ color: "#16a34a" }}>{metrics.max_win_streak}</span>
                  </div>
                  <div className="flex justify-between text-sm py-1.5 border-b border-gray-50">
                    <span style={{ color: "#6b7280" }}>Worst Loss Streak</span>
                    <span className="font-semibold" style={{ color: "#dc2626" }}>{metrics.max_loss_streak}</span>
                  </div>
                  <div className="flex justify-between text-sm py-1.5 border-b border-gray-50">
                    <span style={{ color: "#6b7280" }}>Best Trade</span>
                    <span className="font-semibold" style={{ color: "#16a34a" }}>₹{metrics.best_trade?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between text-sm py-1.5">
                    <span style={{ color: "#6b7280" }}>Worst Trade</span>
                    <span className="font-semibold" style={{ color: "#dc2626" }}>₹{metrics.worst_trade?.toFixed(2)}</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Trade log */}
            {(() => {
              const filtered = (result.trades ?? []).filter((t: any) => {
                const pnl = t.pnl ?? 0;
                if (filterExit !== "All" && t.exit_reason !== filterExit) return false;
                if (filterResult === "Win"  && pnl <= 0) return false;
                if (filterResult === "Loss" && pnl >= 0) return false;
                if (filterSearch && !t.symbol?.toLowerCase().includes(filterSearch.toLowerCase())) return false;
                return true;
              });
              return (
                <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                  {/* Filter bar */}
                  <div className="px-5 py-4 border-b border-gray-100 flex flex-wrap items-center gap-3">
                    <span className="text-sm font-bold text-gray-800">Trade Log</span>
                    <span className="text-xs text-gray-400">{filtered.length} / {result.trades?.length} trades</span>
                    <div className="ml-auto flex items-center gap-2 flex-wrap">
                      <input
                        type="text" placeholder="Search symbol..."
                        value={filterSearch} onChange={e => setFilterSearch(e.target.value)}
                        className="text-xs border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-indigo-400 w-36"
                      />
                      {[
                        { label: "Exit", value: filterExit, set: setFilterExit, options: ["All","TP","SL","EOD"] },
                        { label: "Result", value: filterResult, set: setFilterResult, options: ["All","Win","Loss"] },
                      ].map(({ label, value, set, options }) => (
                        <select key={label} value={value} onChange={e => set(e.target.value)}
                          className="text-xs border border-gray-200 rounded-lg px-2 py-1.5 outline-none focus:border-indigo-400 bg-white text-gray-600">
                          {options.map(o => <option key={o}>{o}</option>)}
                        </select>
                      ))}
                      {(filterExit !== "All" || filterResult !== "All" || filterSearch) && (
                        <button onClick={() => { setFilterExit("All"); setFilterResult("All"); setFilterSearch(""); }}
                          className="text-xs text-indigo-600 hover:text-indigo-800 font-medium">Clear</button>
                      )}
                    </div>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50 border-b border-gray-100">
                          {["Option","Strike","Expiry","DTE","Entry Time","Spot In","Prem In","Spot Out","Prem Out","Exit","Lots","Score","Gross P&L","Charges","Net P&L","Equity"].map(h => (
                            <th key={h} className="px-3 py-2.5 text-left text-xs text-gray-500 font-semibold uppercase whitespace-nowrap">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {filtered.map((t: any, i: number) => {
                          const pnl   = t.pnl        ?? 0;
                          const gross = t.pnl_gross  ?? pnl;
                          const chg   = t.charges    ?? 0;
                          const isCE  = t.option_type === "CE";
                          const isBuy = t.side === "BUY";
                          const chgBreak = t.charges_breakdown;
                          return (
                            <tr key={i} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">

                              {/* Option type chip — CE green, PE orange */}
                              <td className="px-3 py-2.5">
                                <span className="text-xs font-bold px-2 py-0.5 rounded-full"
                                      style={{
                                        background: isCE ? "#dcfce7" : "#fff7ed",
                                        color:      isCE ? "#15803d" : "#c2410c",
                                      }}>
                                  {t.symbol} {t.option_type ?? t.side}
                                </span>
                              </td>

                              {/* Strike */}
                              <td className="px-3 py-2.5 font-mono text-gray-700 text-xs">
                                {t.strike ? Number(t.strike).toLocaleString("en-IN") : "—"}
                              </td>

                              {/* Expiry + DTE */}
                              <td className="px-3 py-2.5 text-xs text-gray-500 font-mono whitespace-nowrap">
                                {t.expiry ?? "—"}
                              </td>
                              <td className="px-3 py-2.5 text-xs text-center">
                                {t.dte_at_entry != null ? (
                                  <span className="px-1.5 py-0.5 rounded font-semibold"
                                        style={{
                                          background: t.dte_at_entry >= 7 ? "#eff6ff" : "#fff7ed",
                                          color:      t.dte_at_entry >= 7 ? "#2563eb" : "#c2410c",
                                        }}>
                                    {t.dte_at_entry}d
                                  </span>
                                ) : "—"}
                              </td>

                              {/* Entry */}
                              <td className="px-3 py-2.5 text-gray-400 text-xs font-mono whitespace-nowrap">
                                {t.entry_time?.slice(11, 19)}
                              </td>
                              <td className="px-3 py-2.5 text-gray-700 text-xs">
                                ₹{Number(t.entry_price).toLocaleString("en-IN")}
                              </td>
                              <td className="px-3 py-2.5 text-indigo-500 text-xs font-semibold">
                                {t.entry_premium ? `₹${t.entry_premium}` : "—"}
                              </td>

                              {/* Exit */}
                              <td className="px-3 py-2.5 text-gray-700 text-xs">
                                ₹{Number(t.exit_price).toLocaleString("en-IN")}
                              </td>
                              <td className="px-3 py-2.5 text-xs font-semibold"
                                  style={{ color: pnl >= 0 ? "#16a34a" : "#dc2626" }}>
                                {t.exit_premium ? `₹${t.exit_premium}` : "—"}
                              </td>

                              {/* Exit reason */}
                              <td className="px-3 py-2.5">
                                <span className="text-xs px-2 py-0.5 rounded-full font-medium"
                                      style={{
                                        background: t.exit_reason === "TP" ? "#dcfce7" : t.exit_reason === "SL" ? "#fee2e2" : "#f3f4f6",
                                        color:      t.exit_reason === "TP" ? "#16a34a" : t.exit_reason === "SL" ? "#dc2626" : "#6b7280",
                                      }}>
                                  {t.exit_reason}
                                </span>
                              </td>

                              <td className="px-3 py-2.5 text-xs">
                                <span className="font-bold text-gray-800">{t.quantity}</span>
                                <span className="text-gray-400 ml-0.5">lots</span>
                              </td>
                              <td className="px-3 py-2.5 text-gray-500 text-xs">{t.score}</td>

                              {/* Gross P&L */}
                              <td className="px-3 py-2.5 text-xs"
                                  style={{ color: gross >= 0 ? "#16a34a" : "#dc2626" }}>
                                {gross >= 0 ? "+" : ""}₹{gross.toFixed(0)}
                              </td>

                              {/* Charges with tooltip breakdown */}
                              <td className="px-3 py-2.5 text-xs text-red-500 font-medium">
                                <span title={chgBreak
                                  ? `Brokerage: ₹${chgBreak.brokerage} | STT: ₹${chgBreak.stt} | Exchange: ₹${chgBreak.exchange} | Stamp: ₹${chgBreak.stamp} | GST: ₹${chgBreak.gst}`
                                  : ""}
                                  className="cursor-help border-b border-dashed border-red-300">
                                  −₹{chg.toFixed(0)}
                                </span>
                              </td>

                              {/* Net P&L */}
                              <td className="px-3 py-2.5 font-bold text-xs"
                                  style={{ color: pnl >= 0 ? "#16a34a" : "#dc2626" }}>
                                {pnl >= 0 ? "+" : ""}₹{pnl.toFixed(0)}
                              </td>

                              <td className="px-3 py-2.5 text-gray-600 text-xs">₹{Number(t.equity).toLocaleString("en-IN")}</td>
                            </tr>
                          );
                        })}
                        {filtered.length === 0 && (
                          <tr><td colSpan={16} className="px-4 py-10 text-center text-gray-400 text-sm">No trades match the filter.</td></tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })()}
          </>
        )}

        {metrics?.error && (
          <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400 text-sm">
            {metrics.error}
          </div>
        )}
      </div>
    </div>
  );
}
