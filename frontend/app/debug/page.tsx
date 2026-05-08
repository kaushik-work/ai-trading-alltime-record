"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function DebugPage() {
  const router = useRouter();
  const [data, setData] = useState<any>(null);
  const [preflight, setPreflight] = useState<any>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [lastFetch, setLastFetch] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runPreflight() {
    const token = localStorage.getItem("aq_token");
    if (!token) { router.replace("/login"); return; }
    setPreflightLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/live/preflight`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: "NIFTY", side: "BUY", option_type: "CE" }),
      });
      const json = await res.json();
      setPreflight(json);
    } catch (e: any) {
      setPreflight({ ok: false, error: e.message, checks: [] });
    } finally {
      setPreflightLoading(false);
    }
  }

  const fetchDebug = useCallback(async () => {
    const token = localStorage.getItem("aq_token");
    if (!token) { router.replace("/login"); return; }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/bot/debug`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401) { router.replace("/login"); return; }
      const json = await res.json();
      setData(json);
      setLastFetch(new Date().toLocaleTimeString("en-IN"));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    if (!localStorage.getItem("aq_token")) { router.replace("/login"); return; }
    fetchDebug();
    const t = setInterval(fetchDebug, 30000);
    return () => clearInterval(t);
  }, [fetchDebug]);

  const strategies = data?.strategies ?? {};

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode="paper" connected={true} botStatus={data ? "running" : "unknown"} onBotToggle={() => {}} />

      <div className="max-w-5xl mx-auto w-full p-3 md:p-6">
        {/* Title row */}
        <div className="flex items-center justify-between mb-4 md:mb-6">
          <div>
            <h1 className="text-lg md:text-xl font-bold text-gray-900">Signal Radar</h1>
            <p className="text-xs text-gray-400 mt-0.5">Live signal scores — no trades placed</p>
          </div>
          <div className="flex items-center gap-2 md:gap-3">
            {lastFetch && <span className="hidden sm:inline text-xs text-gray-400">Updated: {lastFetch}</span>}
            <button
              onClick={fetchDebug}
              disabled={loading}
              className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 border border-gray-300 rounded-lg bg-white text-gray-600 hover:border-gray-400 hover:text-gray-800 transition-colors disabled:opacity-50"
            >
              {loading
                ? <span className="w-3 h-3 border-2 border-gray-400 border-t-transparent rounded-full animate-spin inline-block" />
                : <span style={{ display: "inline-block" }}>↻</span>
              }
              Refresh
            </button>
          </div>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4 mb-4 text-sm text-red-600">{error}</div>
        )}

        {data?.latest_order_issue && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4 mb-4">
            <div className="text-[10px] text-red-500 uppercase font-semibold mb-1">Latest Live Order Issue</div>
            <div className="text-sm font-bold text-red-700">{data.latest_order_issue.error}</div>
            <div className="text-xs text-red-500 mt-1">
              {data.latest_order_issue.symbol || "—"}
              {data.latest_order_issue.detail ? ` · ${data.latest_order_issue.detail}` : ""}
              {data.latest_order_issue.timestamp ? ` · ${new Date(data.latest_order_issue.timestamp).toLocaleTimeString("en-IN")}` : ""}
            </div>
          </div>
        )}

        <div className="bg-white rounded-xl border border-gray-200 p-4 mb-4 md:mb-6">
          <div className="flex items-center justify-between gap-3 mb-3">
            <div>
              <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Live Preflight</div>
              <div className="text-sm text-gray-600">Checks token, contract, LTP, lot size, and estimated margins before live placement.</div>
            </div>
            <button
              onClick={runPreflight}
              disabled={preflightLoading}
              className="text-xs font-semibold px-3 py-2 rounded-lg border border-gray-300 bg-white text-gray-700 hover:border-gray-400 disabled:opacity-50"
            >
              {preflightLoading ? "Running…" : "Run Preflight"}
            </button>
          </div>

          {preflight && (
            <div>
              <div className="flex items-center gap-2 mb-3">
                <span className="text-xs font-bold px-2 py-1 rounded-full"
                      style={preflight.ok ? { background: "#dcfce7", color: "#15803d" } : { background: "#fee2e2", color: "#dc2626" }}>
                  {preflight.ok ? "READY" : "BLOCKED"}
                </span>
                {preflight.error && <span className="text-xs text-red-500">{preflight.error}</span>}
              </div>
              {preflight.resolved && (
                <div className="text-xs text-gray-500 mb-3">
                  {preflight.resolved.tradingsymbol || "—"}
                  {preflight.resolved.expiry ? ` · exp ${preflight.resolved.expiry}` : ""}
                  {preflight.resolved.price ? ` · ₹${Number(preflight.resolved.price).toFixed(2)}` : ""}
                  {preflight.resolved.margin_required ? ` · margin ₹${Number(preflight.resolved.margin_required).toLocaleString("en-IN")}` : ""}
                </div>
              )}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {(preflight.checks ?? []).map((c: any, idx: number) => (
                  <div key={idx} className="rounded-lg border px-3 py-2"
                       style={c.ok ? { borderColor: "#bbf7d0", background: "#f0fdf4" } : { borderColor: "#fecaca", background: "#fef2f2" }}>
                    <div className="text-xs font-semibold" style={{ color: c.ok ? "#15803d" : "#dc2626" }}>
                      {c.ok ? "PASS" : "FAIL"} · {c.name}
                    </div>
                    <div className="text-xs text-gray-600 mt-0.5">{c.detail}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Market status + heartbeat */}
        {data && (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
              <div className="bg-white rounded-xl border border-gray-200 p-4">
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Market</div>
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full inline-block"
                        style={{ background: data.market_open ? "#22c55e" : "#9ca3af" }} />
                  <span className="text-sm font-bold" style={{ color: data.market_open ? "#15803d" : "#6b7280" }}>
                    {data.market_open ? "OPEN" : "CLOSED"}
                  </span>
                </div>
              </div>
              <div className="bg-white rounded-xl border border-gray-200 p-4">
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Server Time (IST)</div>
                <div className="text-sm font-bold text-gray-800">
                  {data.time_ist ? new Date(data.time_ist).toLocaleTimeString("en-IN") : "—"}
                </div>
              </div>
              <div className="bg-white rounded-xl border border-gray-200 p-4">
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Last Heartbeat</div>
                <div className="text-sm font-bold text-gray-800">
                  {data.last_heartbeat ? new Date(data.last_heartbeat).toLocaleTimeString("en-IN") : "Never"}
                </div>
              </div>
            </div>

            {/* Token + VIX status row */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4 md:mb-6">
              {/* Angel One session */}
              <div className="bg-white rounded-xl border border-gray-200 p-4">
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Angel One Session</div>
                {data.token_set_at?.live ? (
                  <div className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full bg-green-500 inline-block flex-shrink-0" />
                    <span className="text-sm font-bold text-gray-800">
                      Live{data.token_set_at.set_at ? ` · set ${new Date(data.token_set_at.set_at).toLocaleTimeString("en-IN")}` : ""}
                    </span>
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full bg-red-500 inline-block flex-shrink-0" />
                    <span className="text-sm font-bold text-red-500">Expired — click Session button</span>
                  </div>
                )}
              </div>

              {/* India VIX */}
              <div className="bg-white rounded-xl border border-gray-200 p-4" style={data.vix_blocked ? { borderColor: "#fed7aa" } : {}}>
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">India VIX</div>
                {data.india_vix != null ? (
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold text-gray-800">{data.india_vix.toFixed(1)}</span>
                    <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full"
                          style={{ background: data.vix_blocked ? "#ffedd5" : "#dcfce7", color: data.vix_blocked ? "#ea580c" : "#15803d" }}>
                      {data.vix_blocked ? `BLOCKED (threshold ${data.vix_threshold})` : "TRADEABLE"}
                    </span>
                  </div>
                ) : (
                  <span className="text-sm text-gray-400">—</span>
                )}
              </div>
            </div>
          </>
        )}

        {/* Strategy cards */}
        <div className="space-y-4">
          {[
            { name: "ATR Intraday", tag: "ATR", color: "#6366f1", bg: "#eef2ff", interval: "5m", type: "Technical (sections 1–11)" },
          ].map(({ name, tag, color, bg, interval, type }) => {
            const s = strategies[name];
            return (
              <div key={name} className="bg-white rounded-xl border border-gray-200 p-5">
                {/* Header */}
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-3">
                    <span className="text-base font-bold px-2 py-0.5 rounded" style={{ background: bg, color }}>
                      {tag}
                    </span>
                    <div>
                      <span className="text-sm font-bold text-gray-900">{name}</span>
                      <span className="ml-2 text-[10px] text-gray-400">{interval} · {type}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {s ? (
                      <ActionBadge action={s.action ?? (s.will_trade ? "TRADE" : "HOLD")} />
                    ) : (
                      <span className="text-xs text-gray-300">no data</span>
                    )}
                  </div>
                </div>

                {!s ? (
                  <div className="text-sm text-gray-400 italic">No signal data yet — refreshes during market hours.</div>
                ) : s.error ? (
                  <div className="text-sm text-red-500">Error: {s.error}</div>
                ) : (
                  <AtrScoreDisplay s={s} color={color} />
                )}
              </div>
            );
          })}
        </div>

        {/* Raw last_scores from runner */}
        {data?.last_scores && Object.keys(data.last_scores).length > 0 && (
          <div className="mt-6 bg-white rounded-xl border border-gray-200 p-4">
            <div className="text-[10px] text-gray-400 uppercase font-semibold mb-3">Last Cycle Scores (from bot runner)</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {Object.entries(data.last_scores).map(([name, sc]: any) => (
                <div key={name} className="bg-gray-50 rounded-lg p-3 text-xs">
                  <div className="font-bold text-gray-700 mb-1">{name}</div>
                  <div className="text-gray-500">Score: <b>{sc.score ?? "—"}</b> · Dir: <b>{sc.direction ?? "—"}</b> · Threshold: <b>{sc.threshold ?? "—"}</b></div>
                  <div className="text-gray-500">Action: <b>{sc.action}</b> · Will Trade: <b>{sc.will_trade ? "YES" : "NO"}</b></div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ActionBadge({ action }: { action: string }) {
  const cfg: Record<string, { bg: string; color: string; label: string }> = {
    BUY:  { bg: "#dcfce7", color: "#15803d", label: "▲ BUY" },
    SELL: { bg: "#fee2e2", color: "#dc2626", label: "▼ SELL" },
    HOLD: { bg: "#f3f4f6", color: "#6b7280", label: "— HOLD" },
    TRADE:{ bg: "#fef3c7", color: "#b45309", label: "⚡ TRADE" },
  };
  const c = cfg[action] ?? cfg.HOLD;
  return (
    <span className="text-[11px] font-bold px-3 py-1 rounded-full"
          style={{ background: c.bg, color: c.color }}>
      {c.label}
    </span>
  );
}

/* ── Score-key → human label + group, in scoring order from signal_scorer.py ─ */
const SCORE_KEY_META: Record<string, { label: string; group: string }> = {
  // 1. Trend (sections 1-3)
  sma50_trend:    { label: "Price vs SMA50",      group: "Trend" },
  sma20_trend:    { label: "Price vs SMA20",      group: "Trend" },
  ema9_momentum:  { label: "Price vs EMA9",       group: "Trend" },
  // 2. Momentum (sections 4-5)
  rsi:            { label: "RSI",                 group: "Momentum" },
  macd:           { label: "MACD",                group: "Momentum" },
  // 3. Volume / Volatility (sections 6, 7, 10)
  volume:         { label: "Volume vs avg",       group: "Vol/Volatility" },
  bollinger:      { label: "Bollinger Bands",     group: "Vol/Volatility" },
  atr_filter:     { label: "ATR vol filter",      group: "Vol/Volatility" },
  // 4. Patterns (section 8)
  patterns:       { label: "Candlestick patterns",group: "Patterns" },
  // 5. Option Chain — section 9 + 9c (the focus the user asked about)
  pcr:            { label: "PCR sentiment",       group: "Option Chain" },
  oc_bias:        { label: "OC bias (CE/PE favored)", group: "Option Chain" },
  ce_wall:        { label: "Near CE wall (resistance)", group: "Option Chain" },
  pe_wall:        { label: "Near PE wall (support)",    group: "Option Chain" },
  oi_delta:       { label: "OI shift (buyers leaving)", group: "Option Chain" },
  oi_delta_hedge: { label: "OI shift (hedging)",  group: "Option Chain" },
  herd_danger:    { label: "🔴 Herd danger (contrarian)", group: "Option Chain" },
  // 6. Intraday levels (section 11)
  vwap:           { label: "VWAP",                group: "Intraday Levels" },
  orb:            { label: "ORB breakout",        group: "Intraday Levels" },
  trend_15m:      { label: "15m trend",           group: "Intraday Levels" },
  rsi_15m:        { label: "15m RSI",             group: "Intraday Levels" },
  pdh_pdl:        { label: "PDH / PDL",           group: "Intraday Levels" },
  // 7. S/R structure (section 12)
  sr_resistance:        { label: "At resistance (CE block)",   group: "S/R Structure" },
  sr_support:           { label: "At support (CE bounce)",     group: "S/R Structure" },
  sr_breakdown:         { label: "Breaking down (CE block)",   group: "S/R Structure" },
  sr_breakup:           { label: "Breaking up (CE go)",        group: "S/R Structure" },
  sr_downtrend:         { label: "Downtrend (CE counter)",     group: "S/R Structure" },
  sr_uptrend_sell:      { label: "Uptrend (PE counter)",       group: "S/R Structure" },
  sr_at_support_sell:   { label: "At support (PE counter)",    group: "S/R Structure" },
  sr_breakup_sell:      { label: "Breaking up (PE counter)",   group: "S/R Structure" },
  sr_resistance_sell:   { label: "At resistance (PE go)",      group: "S/R Structure" },
  sr_breakdown_sell:    { label: "Breaking down (PE go)",      group: "S/R Structure" },
};

const GROUP_ORDER = [
  "Trend", "Momentum", "Vol/Volatility", "Patterns",
  "Option Chain", "Intraday Levels", "S/R Structure",
];

const GROUP_COLORS: Record<string, string> = {
  "Trend":           "#3b82f6",
  "Momentum":        "#8b5cf6",
  "Vol/Volatility":  "#06b6d4",
  "Patterns":        "#f59e0b",
  "Option Chain":    "#ef4444",   // user specifically asked to highlight this
  "Intraday Levels": "#10b981",
  "S/R Structure":   "#6366f1",
};

function AtrScoreDisplay({ s, color }: { s: any; color: string }) {
  const score = s.score ?? 0;
  const breakdown: Record<string, number> = s.breakdown ?? {};

  // Group entries by category and compute per-group totals
  const groups: Record<string, { entries: [string, number][]; total: number }> = {};
  for (const [k, v] of Object.entries(breakdown)) {
    if (typeof v !== "number") continue;
    const meta = SCORE_KEY_META[k] ?? { label: k, group: "Other" };
    if (!groups[meta.group]) groups[meta.group] = { entries: [], total: 0 };
    groups[meta.group].entries.push([k, v]);
    groups[meta.group].total += v;
  }
  const orderedGroups = [...GROUP_ORDER, "Other"].filter(g => groups[g]);

  return (
    <div>
      {/* Score bar */}
      <div className="mb-3">
        <div className="flex justify-between text-xs mb-1">
          <span className="text-gray-500">Signal Score</span>
          <span className="font-bold text-gray-700">{score} / ±10</span>
        </div>
        <div className="h-3 bg-gray-100 rounded-full overflow-hidden relative">
          <div className="absolute top-0 bottom-0 w-0.5 bg-gray-300" style={{ left: "50%" }} />
          <div className="h-full rounded-full transition-all"
               style={{
                 width: `${Math.abs(score) / 10 * 50}%`,
                 marginLeft: score >= 0 ? "50%" : `${50 - Math.abs(score) / 10 * 50}%`,
                 background: score >= 0 ? "#22c55e" : "#ef4444",
               }} />
        </div>
        <div className="flex justify-between text-[9px] text-gray-400 mt-0.5">
          <span>-10 (SELL)</span><span>0</span><span>+10 (BUY)</span>
        </div>
      </div>
      <div className="text-xs text-gray-500 mb-3">
        Direction: <b>{s.direction ?? "—"}</b> · Threshold: <b>±{s.threshold ?? 7}</b> · Will trade: <b>{s.will_trade ? "YES" : "NO"}</b>
      </div>

      {/* Score breakdown — grouped, with group totals.  Option Chain row is
          color-coded red so the user can see the magnitude of OI/PCR/herd
          influence at a glance. */}
      {orderedGroups.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-3 space-y-2.5">
          <div className="text-[10px] font-bold text-gray-400 uppercase tracking-wide">
            Score Breakdown — how each section voted
          </div>
          {orderedGroups.map(g => {
            const { entries, total } = groups[g];
            const gColor = GROUP_COLORS[g] || "#6b7280";
            const totalColor = total > 0 ? "#16a34a" : total < 0 ? "#dc2626" : "#6b7280";
            return (
              <div key={g} className="bg-gray-50 rounded-lg p-2.5">
                <div className="flex items-center justify-between mb-1.5">
                  <div className="flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: gColor }} />
                    <span className="text-xs font-bold text-gray-700">{g}</span>
                  </div>
                  <span className="text-xs font-bold" style={{ color: totalColor }}>
                    {total > 0 ? "+" : ""}{total}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {entries.map(([k, v]) => {
                    const meta = SCORE_KEY_META[k] ?? { label: k, group: g };
                    const pillColor = v > 0 ? "#16a34a" : v < 0 ? "#dc2626" : "#6b7280";
                    const pillBg    = v > 0 ? "#dcfce7" : v < 0 ? "#fee2e2" : "#f3f4f6";
                    return (
                      <span key={k}
                            className="text-[10px] px-2 py-0.5 rounded font-medium"
                            style={{ background: pillBg, color: pillColor }}
                            title={k}>
                        {meta.label}: {v > 0 ? "+" : ""}{v}
                      </span>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Latest signal log lines */}
      {Array.isArray(s.signals) && s.signals.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-3">
          <div className="text-[10px] font-bold text-gray-400 uppercase tracking-wide mb-1">
            Signals fired this cycle
          </div>
          <ul className="text-[10px] text-gray-500 space-y-0.5">
            {s.signals.slice(0, 5).map((sig: string, i: number) => (
              <li key={i} className="truncate">· {sig}</li>
            ))}
          </ul>
        </div>
      )}

      {s.note && <div className="text-[10px] text-gray-400 mt-2">{s.note}</div>}
    </div>
  );
}

