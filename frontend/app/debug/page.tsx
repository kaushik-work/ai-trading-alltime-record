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
  const [vixSaving, setVixSaving] = useState<Record<string, boolean>>({});

  async function toggleVix(endpoint: string, currentState: boolean) {
    const key = endpoint;
    setVixSaving(s => ({ ...s, [key]: true }));
    try {
      const token = localStorage.getItem("aq_token");
      await fetch(`${API_URL}${endpoint}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ enable: !currentState }),
      });
      await fetchDebug();
    } finally {
      setVixSaving(s => ({ ...s, [key]: false }));
    }
  }

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
              {/* Zerodha token */}
              <div className="bg-white rounded-xl border border-gray-200 p-4">
                <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">Zerodha Token</div>
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
                    <span className="text-sm font-bold text-red-500">Expired — run get_token.py</span>
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
            { name: "ATR Intraday", tag: "ATR", color: "#6366f1", bg: "#eef2ff", interval: "5m",  type: "Technical (sections 1–11)", vixEndpoint: "/api/bot/vix-override/atr", vixKey: "vix_override_atr" },
            { name: "C-ICT",        tag: "ICT", color: "#0891b2", bg: "#e0f2fe", interval: "5m",  type: "Order Blocks + Liquidity (section 12)", vixEndpoint: "/api/bot/vix-override/ict", vixKey: "vix_override_ict" },
            { name: "Fib-OF",       tag: "FIB", color: "#059669", bg: "#d1fae5", interval: "15m", type: "Fibonacci zones + Order Flow", vixEndpoint: "/api/bot/vix-override/fib", vixKey: "vix_override_fib" },
          ].map(({ name, tag, color, bg, interval, type, vixEndpoint, vixKey }) => {
            const s = strategies[name];
            const vixOn: boolean = data?.[vixKey] ?? false;
            const vixBusy = vixSaving[vixEndpoint] ?? false;
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
                    {/* Per-strategy VIX toggle */}
                    <button
                      onClick={() => toggleVix(vixEndpoint, vixOn)}
                      disabled={vixBusy}
                      className="text-[10px] font-semibold px-2 py-1 rounded-full border transition-colors disabled:opacity-50"
                      style={vixOn
                        ? { background: "#fef3c7", color: "#b45309", borderColor: "#f59e0b" }
                        : { background: "#f3f4f6", color: "#6b7280", borderColor: "#d1d5db" }
                      }
                      title={vixOn ? "VIX gate bypassed for this strategy — click to restore" : "VIX gate active — click to bypass"}
                    >
                      {vixOn ? "Override ON" : "Override OFF"}
                    </button>
                    {s ? (
                      <ActionBadge action={s.action ?? (s.will_trade ? "TRADE" : "HOLD")} />
                    ) : (
                      <span className="text-xs text-gray-300">no data</span>
                    )}
                  </div>
                </div>

                {!s ? (
                  <div className="text-sm text-gray-400 italic">No signal data yet. Hit refresh during market hours.</div>
                ) : s.error ? (
                  <div className="text-sm text-red-500">Error: {s.error}</div>
                ) : name === "C-ICT" ? (
                  <IctScoreDisplay s={s} color={color} />
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
                  <div className="text-gray-500">Buy: <b>{sc.buy}</b> · Sell: <b>{sc.sell}</b> · Threshold: <b>{sc.threshold}</b></div>
                  <div className="text-gray-500">Action: <b>{sc.action}</b> · now_t: {sc.now_t} · bar_time: {sc.bar_time}</div>
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

function ScoreBar({ label, score, max, color }: { label: string; score: number; max: number; color: string }) {
  const pct = Math.min((score / max) * 100, 100);
  return (
    <div className="mb-2">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-500 font-medium">{label}</span>
        <span className="font-bold text-gray-700">{score.toFixed(1)} / {max}</span>
      </div>
      <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all"
             style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

function ScoreDisplay({ s, color }: { s: any; color: string }) {
  const threshold = s.threshold ?? 8.5;
  const threshPct = (threshold / 10) * 100;
  return (
    <div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
        <div>
          <ScoreBar label="Buy Score (CE)" score={s.buy_score ?? 0} max={10} color="#22c55e" />
          <ScoreBar label="Sell Score (PE)" score={s.sell_score ?? 0} max={10} color="#ef4444" />
        </div>
        <div className="bg-gray-50 rounded-lg p-3">
          <div className="text-[10px] text-gray-400 uppercase font-semibold mb-2">Threshold</div>
          <div className="relative h-3 bg-gray-200 rounded-full mb-2">
            <div className="absolute top-0 bottom-0 rounded-full" style={{ left: 0, width: `${threshPct}%`, background: color, opacity: 0.3 }} />
            <div className="absolute top-1/2 -translate-y-1/2 w-0.5 h-4 rounded bg-gray-600" style={{ left: `${threshPct}%` }} />
          </div>
          <div className="text-xs text-gray-500">Need ≥ <b>{threshold}</b> to trade</div>
          {s.bars && <div className="text-xs text-gray-400 mt-1">{s.bars} bars · bar_time {s.bar_time_ist}</div>}
        </div>
      </div>
      {s.details && Object.keys(s.details).length > 0 && (
        <div>
          <div className="text-[10px] text-gray-400 uppercase font-semibold mb-2">Score Breakdown</div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(s.details).map(([k, v]: any) => (
              <span key={k} className="text-[10px] px-2 py-0.5 rounded font-medium"
                    style={{ background: v > 0 ? "#dcfce7" : "#f3f4f6", color: v > 0 ? "#15803d" : "#6b7280" }}>
                {k}: +{v}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function IctScoreDisplay({ s, color }: { s: any; color: string }) {
  const score = s.score ?? 0;
  const of = s.order_flow ?? {};
  return (
    <div>
      <div className="mb-3">
        <div className="flex justify-between text-xs mb-1">
          <span className="text-gray-500">ICT Signal Score</span>
          <span className="font-bold text-gray-700">{score > 0 ? "+" : ""}{score} / ±4</span>
        </div>
        <div className="h-3 bg-gray-100 rounded-full overflow-hidden relative">
          <div className="absolute top-0 bottom-0 w-0.5 bg-gray-300" style={{ left: "50%" }} />
          <div className="h-full rounded-full transition-all"
               style={{
                 width: `${Math.abs(score) / 4 * 50}%`,
                 marginLeft: score >= 0 ? "50%" : `${50 - Math.abs(score) / 4 * 50}%`,
                 background: score >= 0 ? "#22c55e" : "#ef4444",
               }} />
        </div>
        <div className="flex justify-between text-[9px] text-gray-400 mt-0.5">
          <span>-4 (SELL)</span><span>0</span><span>+4 (BUY)</span>
        </div>
      </div>
      <div className="text-xs text-gray-500 mb-2">
        Direction: <b>{s.direction ?? "—"}</b> · Threshold: <b>±{s.threshold ?? 2}</b> · Will trade: <b>{s.will_trade ? "YES" : "NO"}</b>
      </div>
      {(of.ict_liq_score != null || of.ict_ob_score != null) && (
        <div className="flex flex-wrap gap-2 text-[10px]">
          {of.ict_liq_score != null && (
            <span className="px-2 py-0.5 rounded font-medium"
                  style={{ background: of.ict_liq_score > 0 ? "#dcfce7" : of.ict_liq_score < 0 ? "#fee2e2" : "#f3f4f6",
                           color:      of.ict_liq_score > 0 ? "#15803d" : of.ict_liq_score < 0 ? "#dc2626" : "#6b7280" }}>
              Liq sweep: {of.ict_liq_score > 0 ? "+" : ""}{of.ict_liq_score}
            </span>
          )}
          {of.ict_ob_score != null && (
            <span className="px-2 py-0.5 rounded font-medium"
                  style={{ background: of.ict_ob_score > 0 ? "#dcfce7" : of.ict_ob_score < 0 ? "#fee2e2" : "#f3f4f6",
                           color:      of.ict_ob_score > 0 ? "#15803d" : of.ict_ob_score < 0 ? "#dc2626" : "#6b7280" }}>
              OB retest: {of.ict_ob_score > 0 ? "+" : ""}{of.ict_ob_score}
            </span>
          )}
        </div>
      )}
      {s.note && <div className="text-[10px] text-gray-400 mt-1">{s.note}</div>}
    </div>
  );
}

function AtrScoreDisplay({ s, color }: { s: any; color: string }) {
  const score = s.score ?? 0;
  const pct = ((score + 10) / 20) * 100;
  return (
    <div>
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
      <div className="text-xs text-gray-500">
        Direction: <b>{s.direction ?? "—"}</b> · Threshold: <b>±{s.threshold ?? 7}</b> · Will trade: <b>{s.will_trade ? "YES" : "NO"}</b>
      </div>
      {s.note && <div className="text-[10px] text-gray-400 mt-1">{s.note}</div>}
    </div>
  );
}
