"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "./components/Header";
import CryptoChart from "./CryptoChart";
import NseView from "./NseView";

const _API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const _WS  = _API.replace(/^http/, "ws");

type KillResult = {
  ok: boolean;
  killed_strategies?: string[];
  open_after?: string[];
  kill_switch_armed?: boolean;
  message?: string;
  error?: string;
};

type SignalRow = {
  underlying: string;
  spot: number;
  width_pct: number;
  r_high: number;
  r_low: number;
  trend: "bullish" | "bearish" | "neutral";
  trend_ma?: number;
  near_support: boolean;
  near_resistance: boolean;
  wick_touch_support: boolean;
  wick_touch_resistance: boolean;
  strong_green: boolean;
  strong_red: boolean;
  in_cooldown: boolean;
  time_ok?: boolean;
  block_long?: boolean;
  block_short?: boolean;
  sl_pct: number;
  tp_pct: number;
  vol_24h?: number;
  vol_filter_ok?: boolean;
  candles_count?: number;
  warmup_target?: number;
  warmup_pct?: number;
  side: "buy" | "sell" | null;
  ready: boolean;
};

type PortfolioState = {
  wallet_usd: number | null;
  wallet_inr?: number | null;
  wallet_pool_usd?: number | null;   // total tradeable pool (USD + INR-converted)
  capital_use_pct?: number;           // fraction of pool deployed per cycle
  fixed_capital_mode?: boolean;
  fixed_capital_inr?: number | null;
  day_pnl: number;
  open_positions: number;
  killed?: boolean;
  mode?: string;
};

type StreamDiag = {
  connected: boolean;
  marks_fresh?: number;
  marks_total?: number;
  last_msg_age_s?: number | null;
};

type ShadowTrade = {
  id: string;
  entry_ts: string;
  strategy: string;
  symbol: string;
  side: string;
  entry_px: number;
  width_pct: number;
  size_mult: number;
  status: "open" | "closed";
  peak_pct: number;
  exit_ts?: string;
  exit_px?: number;
  pnl_pct?: number;
  held_hours?: number;
  exit_reason?: string;
};

type ShadowSummary = {
  open: number;
  closed: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pct: number;
  avg_win_pct: number;
  avg_loss_pct: number;
};

type MissedSignal = {
  id: string;
  ts: string;
  strategy: string;
  symbol: string;
  side: string;
  width_pct: number;
  reason: string;
  detail: string;
};

type Snapshot = {
  ts: string;
  perp_marks: Record<string, number>;
  shadow_trades?: ShadowTrade[];
  shadow_summary?: ShadowSummary;
  missed_signals?: MissedSignal[];
  signals: SignalRow[];
  portfolio: PortfolioState;
  stream: StreamDiag;
};

function signalState(s: SignalRow): { label: string; color: string; hex: string } {
  if (s.side === "buy") return { label: "LONG", color: "text-green-400", hex: "#4ade80" };
  if (s.side === "sell") return { label: "SHORT", color: "text-red-400", hex: "#f87171" };
  if (!s.ready) return { label: "warmup", color: "text-gray-500", hex: "#6b7280" };
  if (s.in_cooldown) return { label: "cooldown", color: "text-gray-500", hex: "#6b7280" };
  if (s.wick_touch_support && s.trend === "bullish") return { label: "armed long", color: "text-green-600", hex: "#16a34a" };
  if (s.wick_touch_resistance && s.trend === "bearish") return { label: "armed short", color: "text-red-600", hex: "#dc2626" };
  if (s.near_support && s.trend === "bullish") return { label: "near support", color: "text-green-700", hex: "#15803d" };
  if (s.near_resistance && s.trend === "bearish") return { label: "near resistance", color: "text-red-700", hex: "#b91c1c" };
  return { label: "flat", color: "text-gray-500", hex: "#6b7280" };
}

function signalTooltips(s: SignalRow) {
  const ma = s.trend_ma ?? 0;
  const trendReason = s.trend === "bullish"
    ? `Spot $${s.spot.toLocaleString()} is above 24h MA $${ma.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : s.trend === "bearish"
    ? `Spot $${s.spot.toLocaleString()} is below 24h MA $${ma.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : "Trend unclear — price near 24h MA";

  const parts: string[] = [];
  if (!s.ready) parts.push("Strategy still in warmup");
  if (s.in_cooldown) parts.push("In 1h post-trade cooldown");
  if (s.block_long) parts.push("Long side blocked after recent loss");
  if (s.block_short) parts.push("Short side blocked after recent loss");
  if (s.time_ok === false) parts.push("Outside configured trading hours");
  if (s.vol_filter_ok === false) parts.push(`24h vol ${((s.vol_24h ?? 0) * 100).toFixed(1)}% > 34% filter`);
  if (!s.near_support && !s.near_resistance) parts.push("Price not near 4h S/R edge");
  else if (s.near_support && s.trend !== "bullish") parts.push("Near support but trend is not bullish");
  else if (s.near_resistance && s.trend !== "bearish") parts.push("Near resistance but trend is not bearish");
  if (s.near_support && s.trend === "bullish" && !s.wick_touch_support) parts.push("No wick touch at support yet");
  if (s.near_resistance && s.trend === "bearish" && !s.wick_touch_resistance) parts.push("No wick touch at resistance yet");
  if (s.wick_touch_support && s.trend === "bullish" && !s.strong_green) parts.push("Wick touched support but candle not strong green");
  if (s.wick_touch_resistance && s.trend === "bearish" && !s.strong_red) parts.push("Wick touched resistance but candle not strong red");
  if (parts.length === 0 && s.side == null) parts.push("No S/R retest setup currently");

  return {
    spot: `ETHUSD mark price from Delta stream`,
    range: `4h S/R range width. High $${(s.r_high ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}, Low $${(s.r_low ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`,
    trend: trendReason,
    state: parts.join(" • ") || "Strategy state",
    sl: `Stop loss distance per ETH trade (0.7%)`,
    tp: `Take profit distance per ETH trade (4.9%, 1:7 R:R)`,
    vol: `Annualized 24h realized volatility from 1m returns. Filter max = 34%`,
  };
}

function isTokenValid(token: string | null): boolean {
  if (!token) return false;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 > Date.now() + 30_000; // 30s buffer
  } catch {
    return false;
  }
}

function logoutAndLogin(router: ReturnType<typeof useRouter>) {
  localStorage.removeItem("aq_token");
  router.replace("/login");
}

export default function CryptoHome() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [viewMode, setViewMode] = useState<"crypto" | "nse">("crypto");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");
  const [killConfirm, setKillConfirm] = useState(false);
  const [killBusy, setKillBusy] = useState(false);
  const [killResult, setKillResult] = useState<KillResult | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<any>(null);

  async function handleKill() {
    setKillBusy(true);
    setKillResult(null);
    try {
      const token = localStorage.getItem("aq_token");
      const r = await fetch(`${_API}/api/crypto/kill`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      const j: KillResult = await r.json();
      setKillResult(j);
    } catch (e: any) {
      setKillResult({ ok: false, error: e?.message || "Network error" });
    } finally {
      setKillBusy(false);
      setKillConfirm(false);
    }
  }

  useEffect(() => {
    const token = localStorage.getItem("aq_token");
    if (!isTokenValid(token)) {
      logoutAndLogin(router);
    } else {
      setAuthed(true);
    }
  }, []);

  // WebSocket subscription — replaces the old 60s polling
  useEffect(() => {
    if (!authed) return;

    const connect = () => {
      const token = localStorage.getItem("aq_token");
      if (!isTokenValid(token)) {
        logoutAndLogin(router);
        return;
      }
      setWsState("connecting");
      const ws = new WebSocket(`${_WS}/ws/crypto?token=${encodeURIComponent(token || "")}`);
      wsRef.current = ws;
      ws.onopen = () => setWsState("open");
      ws.onmessage = (ev) => {
        try { setSnap(JSON.parse(ev.data) as Snapshot); }
        catch { /* malformed payload */ }
      };
      ws.onclose = (ev) => {
        setWsState("closed");
        wsRef.current = null;
        // 1008 = policy violation = invalid/expired token (server sends "Invalid or expired token")
        if (ev.code === 1008) {
          logoutAndLogin(router);
          return;
        }
        // Reconnect after 3s
        reconnectTimer.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };
    connect();

    // Re-check token when laptop wakes up / tab becomes visible again
    const onVisible = () => {
      if (document.visibilityState === "visible" && !isTokenValid(localStorage.getItem("aq_token"))) {
        logoutAndLogin(router);
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, [authed]);

  if (!authed) return null;

  const signals = snap?.signals ?? [];
  const portfolio = snap?.portfolio;
  const stream = snap?.stream;
  const firing = signals.filter(s => s.side != null);
  const liveEth = snap?.perp_marks?.["ETHUSD"];
  // Strategy state summary for the chart header so it matches Signal Radar.
  const ethSignal = signals[0];
  const chartSignal = ethSignal
    ? {
        stateLabel: signalState(ethSignal).label,
        stateColor: signalState(ethSignal).hex,
        reason: signalTooltips(ethSignal).state,
        trend: ethSignal.trend,
        volFilterOk: ethSignal.vol_filter_ok,
        vol24h: (ethSignal.vol_24h ?? 0) * 100,
      }
    : null;
  const shadowTrades = snap?.shadow_trades ?? [];
  const shadowSummary = snap?.shadow_summary;
  const lastShadow = shadowTrades.length ? shadowTrades[shadowTrades.length - 1] : null;
  const missedSignals = snap?.missed_signals ?? [];
  const lastMissed = missedSignals.length ? missedSignals[missedSignals.length - 1] : null;

  // Max 4h S/R range width across assets — used for the strip stat
  const maxRangePct = signals.length
    ? Math.max(...signals.map(s => Math.abs(s.width_pct)))
    : 0;

  return (
    <div className="min-h-screen bg-[#0a0a14] text-gray-200">
      <Header
        mode={viewMode}
        onModeChange={setViewMode}
        connected={wsState === "open"}
        botStatus={portfolio?.open_positions ? "running" : "idle"}
        onBotToggle={() => { /* crypto bot toggle TBD via /api/crypto/toggle */ }}
        errorCount={0}
        settings={{ min_lots: 1 }}
      />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
      {viewMode === "crypto" ? (
        <>

        {/* Header bar — stacks on mobile, side-by-side on tablet+ */}
        <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-3 mb-6">
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold text-[#627eea]">
              Crypto · Delta India
            </h1>
            <p className="text-xs text-gray-500 mt-1">
              price-action S/R retest · ETH-only · {snap?.ts && `last tick ${new Date(snap.ts).toLocaleTimeString()}`}
              <span className="ml-3">
                ws: <span className={
                  wsState === "open"       ? "text-green-400"
                  : wsState === "connecting" ? "text-yellow-400"
                                              : "text-red-400"
                }>{wsState}</span>
                {stream && (
                  <span className="ml-3 text-gray-600">
                    stream {stream.connected ? "✓" : "✗"} · {stream.marks_fresh ?? 0}/{stream.marks_total ?? 0} fresh
                  </span>
                )}
              </span>
            </p>
          </div>
          <div className="flex items-center gap-2 sm:gap-3 flex-shrink-0">
            <button
              onClick={() => setKillConfirm(true)}
              disabled={killBusy}
              className="px-4 py-2 text-xs font-semibold text-white rounded-lg shadow-md hover:opacity-90 disabled:opacity-50"
              style={{ background: "linear-gradient(135deg,#dc2626 0%,#7f1d1d 100%)" }}
            >
              {killBusy ? "Killing..." : <><span className="hidden sm:inline">🛑 KILL CRYPTO BOT</span><span className="sm:hidden">🛑 KILL</span></>}
            </button>
          </div>
        </div>

        {killResult && (
          <div className={`border rounded-lg p-4 mb-6 ${
            killResult.ok ? "border-red-700 bg-red-950/30" : "border-yellow-700 bg-yellow-950/30"
          }`}>
            <div className="flex items-baseline justify-between">
              <div>
                <p className="font-semibold text-white">
                  {killResult.ok ? "✓ Kill executed" : "✗ Kill failed"}
                </p>
                <p className="text-xs text-gray-400 mt-1">
                  {killResult.message || killResult.error}
                </p>
                {killResult.killed_strategies && killResult.killed_strategies.length > 0 && (
                  <p className="text-xs text-gray-400 mt-1">
                    Closed: {killResult.killed_strategies.join(", ")}
                  </p>
                )}
              </div>
              <button
                onClick={() => setKillResult(null)}
                className="text-xs text-gray-500 hover:text-white"
              >
                dismiss
              </button>
            </div>
          </div>
        )}

        {killConfirm && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
            onClick={() => setKillConfirm(false)}
          >
            <div
              className="bg-[#13131f] border border-red-700 rounded-2xl p-6 max-w-md mx-4"
              onClick={(e) => e.stopPropagation()}
            >
              <h3 className="text-lg font-bold text-red-400 mb-2">
                🛑 Confirm: Kill Crypto Bot
              </h3>
              <p className="text-sm text-gray-300 mb-1">
                This will <strong>immediately close all open crypto positions</strong>
                {" "}at market price and halt new entries until the bot is restarted.
              </p>
              <p className="text-xs text-gray-500 mb-4">
                Use this when you suspect a bug, market event, or want manual control.
                Cannot be undone via UI.
              </p>
              <div className="flex justify-end gap-3 mt-4">
                <button
                  onClick={() => setKillConfirm(false)}
                  className="px-4 py-2 text-sm text-gray-400 hover:text-white border border-[#1e1e30] rounded-lg"
                >
                  Cancel
                </button>
                <button
                  onClick={handleKill}
                  disabled={killBusy}
                  className="px-5 py-2 text-sm font-semibold text-white rounded-lg shadow-md hover:opacity-90 disabled:opacity-50"
                  style={{ background: "linear-gradient(135deg,#dc2626 0%,#7f1d1d 100%)" }}
                >
                  {killBusy ? "Killing..." : "Yes — kill bot"}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Portfolio ribbon — 2 cols on phone, 5 on tablet+ */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 sm:gap-3 mb-6">
          <StatCard
            label="Tradeable Pool"
            value={portfolio?.wallet_pool_usd != null
              ? `$${portfolio.wallet_pool_usd.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
              : portfolio?.mode === "paper" ? "paper" : "—"}
            accent={portfolio?.wallet_pool_usd != null && portfolio.wallet_pool_usd <= 0 ? "red" : undefined}
            footnote={
              portfolio?.fixed_capital_mode
                ? `fixed ₹${portfolio.fixed_capital_inr?.toLocaleString("en-IN")} budget per trade`
                : portfolio?.wallet_inr && portfolio.wallet_inr > 0
                  ? `incl. ₹${portfolio.wallet_inr.toLocaleString("en-IN", { maximumFractionDigits: 0 })} INR (auto-converted)`
                  : portfolio?.wallet_usd != null && portfolio.wallet_usd > 0
                    ? `${(portfolio.capital_use_pct ?? 0.5) * 100}% deployed per cycle`
                    : undefined
            }
          />
          <StatCard label="Today P&L" value={portfolio ? `${portfolio.day_pnl >= 0 ? "+" : ""}$${portfolio.day_pnl.toFixed(0)}` : "—"}
                    accent={portfolio && portfolio.day_pnl > 0 ? "green" : portfolio && portfolio.day_pnl < 0 ? "red" : undefined} />
          <StatCard label="Open positions" value={portfolio ? `${portfolio.open_positions}` : "—"} />
          <StatCard label="Mode" value={portfolio?.mode ?? "—"} accent={portfolio?.mode === "live" ? "green" : undefined} />
          <StatCard label="Max S/R width" value={`${maxRangePct.toFixed(3)}%`} />
        </div>

        {/* Shadow-trade panel — full paper-trading lifecycle.  Tracks each
            would-be entry through stop/TP/trail/max-hold the same way real
            trades are managed, so the user sees what the bot WOULD have made. */}
        {(lastShadow || (shadowSummary && shadowSummary.closed > 0)) && (
          <div className="border border-yellow-700/40 bg-yellow-950/15 rounded-lg p-4 mb-6">
            <div className="flex items-baseline justify-between mb-2">
              <span className="text-yellow-400 text-sm font-semibold">
                ⚡ Shadow Trading — paper P&L while wallet is empty
              </span>
              <span className="text-[10px] text-gray-500">
                latest signal {lastShadow && new Date(lastShadow.entry_ts).toLocaleTimeString()}
              </span>
            </div>
            {shadowSummary && (
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2 sm:gap-3 mb-2 text-xs">
                <div>
                  <p className="text-gray-500">Open</p>
                  <p className="font-semibold text-white">{shadowSummary.open}</p>
                </div>
                <div>
                  <p className="text-gray-500">Closed</p>
                  <p className="font-semibold text-white">
                    {shadowSummary.closed} ({shadowSummary.wins}W / {shadowSummary.losses}L)
                  </p>
                </div>
                <div>
                  <p className="text-gray-500">Win rate</p>
                  <p className="font-semibold text-white">
                    {shadowSummary.win_rate.toFixed(0)}%
                  </p>
                </div>
                <div>
                  <p className="text-gray-500">Avg trade</p>
                  <p className="font-semibold font-mono text-xs">
                    <span className="text-green-400">+{shadowSummary.avg_win_pct.toFixed(2)}%</span>
                    <span className="text-gray-600 mx-1">/</span>
                    <span className="text-red-400">{shadowSummary.avg_loss_pct.toFixed(2)}%</span>
                  </p>
                </div>
                <div>
                  <p className="text-gray-500">Cumulative P&L</p>
                  <p className={`font-semibold font-mono ${
                    shadowSummary.total_pct > 0 ? "text-green-400"
                    : shadowSummary.total_pct < 0 ? "text-red-400" : "text-white"
                  }`}>
                    {shadowSummary.total_pct >= 0 ? "+" : ""}{shadowSummary.total_pct.toFixed(2)}%
                  </p>
                </div>
              </div>
            )}
            {lastShadow && (
              <p className="text-[11px] text-gray-400 font-mono">
                latest: {lastShadow.symbol} {lastShadow.side.toUpperCase()}
                {" @ "}${lastShadow.entry_px.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                {" · width "}{lastShadow.width_pct >= 0 ? "+" : ""}{lastShadow.width_pct.toFixed(3)}%
                {" · size "}{lastShadow.size_mult.toFixed(1)}×
                {lastShadow.status === "closed"
                  ? ` → closed ${lastShadow.exit_reason} ${(lastShadow.pnl_pct ?? 0) >= 0 ? "+" : ""}${(lastShadow.pnl_pct ?? 0).toFixed(2)}%`
                  : " (open)"}
              </p>
            )}
            <p className="text-[10px] text-gray-600 mt-1">
              Fund Delta wallet with INR/USDT to convert these into live orders. Price-action bracket: ETH 0.7% SL / 4.9% TP (1:7), breakeven trail at +1R.
            </p>
          </div>
        )}

        {/* Missed-signals panel — signals that crossed the gate but did NOT
            become live orders (empty wallet, API failure, zero sizing, kill
            switch, no mark).  Pure visibility; no P&L is tracked here. */}
        {missedSignals.length > 0 && (
          <div className="border border-red-700/40 bg-red-950/15 rounded-lg p-4 mb-6">
            <div className="flex items-baseline justify-between mb-2">
              <span className="text-red-400 text-sm font-semibold">
                ⚠️ Missed Signals — entries that did not reach the exchange
              </span>
              <span className="text-[10px] text-gray-500">
                {missedSignals.length} in buffer
              </span>
            </div>
            {lastMissed && (
              <p className="text-[11px] text-gray-400 font-mono mb-2">
                latest: {lastMissed.strategy} {lastMissed.symbol} {lastMissed.side.toUpperCase()}
                {" @ "}{new Date(lastMissed.ts).toLocaleTimeString()}
                {" · width "}{lastMissed.width_pct >= 0 ? "+" : ""}{lastMissed.width_pct.toFixed(3)}%
                {" · reason "}<span className="text-red-300">{lastMissed.reason}</span>
                {lastMissed.detail && ` · ${lastMissed.detail}`}
              </p>
            )}
            <div className="overflow-x-auto -mx-2 sm:mx-0">
              <table className="w-full text-[10px] sm:text-xs min-w-[480px]">
                <thead className="text-gray-500 border-b border-[#1e1e30]">
                  <tr>
                    <th className="text-left py-1 px-2">Time</th>
                    <th className="text-left px-2">Strategy</th>
                    <th className="text-right px-2">Side</th>
                    <th className="text-right px-2">Width</th>
                    <th className="text-left px-2">Reason</th>
                    <th className="text-left px-2">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {[...missedSignals].reverse().slice(0, 10).map((m) => (
                    <tr key={m.id} className="border-b border-[#13131f]">
                      <td className="py-1 px-2 font-mono">{new Date(m.ts).toLocaleTimeString()}</td>
                      <td className="px-2">{m.strategy}</td>
                      <td className="text-right px-2">
                        <span className={m.side === "buy" ? "text-green-400" : m.side === "sell" ? "text-red-400" : "text-gray-400"}>
                          {m.side.toUpperCase()}
                        </span>
                      </td>
                      <td className="text-right px-2 font-mono">{m.width_pct.toFixed(3)}%</td>
                      <td className="px-2 text-red-300">{m.reason}</td>
                      <td className="px-2 text-gray-400 truncate max-w-[200px]">{m.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Live ETH chart — Signal Radar below covers 4h S/R width + retest state */}
        <div className="mb-6">
          <CryptoChart livePrices={{ ETH: liveEth }} signal={chartSignal} />
        </div>

        {/* Signal Radar */}
        <div className="border border-[#1e1e30] rounded-2xl p-6 mb-8 bg-[#0e0e1a]">
          <div className="flex items-baseline justify-between mb-4">
            <h2 className="text-lg font-semibold">Signal Radar</h2>
            <span className="text-xs text-gray-500">
              {firing.length} firing now · wick-touch retest
            </span>
          </div>

          {signals.length === 0 ? (
            <p className="text-sm text-gray-500">
              {wsState === "open" ? "No signals yet — stream warming up." : "Connecting…"}
            </p>
          ) : (
            <>
            {signals.some(s => !s.ready) && (
              <div className="mb-4">
                {signals.filter(s => !s.ready).map((s, i) => (
                  <div key={i} className="mb-2 last:mb-0">
                    <div className="flex justify-between text-xs text-gray-400 mb-1">
                      <span>{s.underlying} warmup</span>
                      <span>{s.candles_count ?? 0} / {s.warmup_target ?? 1440} candles ({s.warmup_pct ?? 0}%)</span>
                    </div>
                    <div className="h-1.5 w-full bg-[#1e1e30] rounded-full overflow-hidden">
                      <div
                        className="h-full bg-[#627eea] transition-all duration-500"
                        style={{ width: `${s.warmup_pct ?? 0}%` }}
                      />
                    </div>
                    <p className="text-[10px] text-gray-500 mt-1">
                      Strategy needs 24h of 1-minute candles before it can generate signals.
                    </p>
                  </div>
                ))}
              </div>
            )}
            <div className="overflow-x-auto -mx-2 sm:mx-0">
            <table className="w-full text-xs sm:text-sm min-w-[560px]">
              <thead className="text-gray-500 border-b border-[#1e1e30]">
                <tr>
                  <th className="text-left py-2 px-2">Asset</th>
                  <th className="text-right px-2">Spot</th>
                  <th className="text-right px-2">4h Range</th>
                  <th className="text-right px-2">Trend</th>
                  <th className="text-right px-2">State</th>
                  <th className="text-right px-2 hidden sm:table-cell">SL</th>
                  <th className="text-right px-2 hidden sm:table-cell">TP</th>
                  <th className="text-right px-2 hidden sm:table-cell" title="24h realized volatility">24h Vol</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => {
                  const state = signalState(s);
                  const fires = s.side != null;
                  const tips = signalTooltips(s);
                  return (
                    <tr key={i}
                        className={`border-b border-[#13131f] ${fires ? "bg-[#f7931a08]" : ""}`}>
                      <td className="py-2 px-2" title={tips.state}>
                        <span className={`inline-block w-2 h-2 rounded-full mr-2 ${
                          fires ? "bg-[#f7931a]" : "bg-gray-700"
                        }`} />
                        {s.underlying}
                      </td>
                      <td className="text-right px-2 cursor-help" title={tips.spot}>
                        ${s.spot.toLocaleString()}
                      </td>
                      <td className="text-right px-2 font-mono cursor-help" title={tips.range}>
                        {s.width_pct.toFixed(3)}%
                      </td>
                      <td className={`text-right px-2 cursor-help ${
                        s.trend === "bullish" ? "text-green-400" :
                        s.trend === "bearish" ? "text-red-400" : "text-gray-400"
                      }`} title={tips.trend}>
                        {s.trend}
                      </td>
                      <td className={`text-right px-2 cursor-help ${state.color}`} title={tips.state}>
                        {state.label}
                      </td>
                      <td className="text-right px-2 hidden sm:table-cell text-red-400 cursor-help" title={tips.sl}>
                        {(s.sl_pct * 100).toFixed(2)}%
                      </td>
                      <td className="text-right px-2 hidden sm:table-cell text-green-400 cursor-help" title={tips.tp}>
                        {(s.tp_pct * 100).toFixed(2)}%
                      </td>
                      <td className={`text-right px-2 hidden sm:table-cell font-mono cursor-help ${
                        (s.vol_24h ?? 0) > 0.34 ? "text-red-400" : "text-gray-300"
                      }`} title={tips.vol}>
                        {((s.vol_24h ?? 0) * 100).toFixed(1)}%
                        {(s.vol_filter_ok === false) && <span className="ml-1 text-[10px] text-red-500">(filtered)</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
            </>
          )}
        </div>

        <p className="text-xs text-gray-500">
          Signal source: price-action S/R retest on Delta India ETHUSD perp.
          Enters at 4h S/R edges in the direction of the 24h trend when the wick
          touches the level and a strong reversal candle forms. Risk controls:
          ETH 0.7% SL / 4.9% TP (1:7), 24h vol filter ≤ 34%, breakeven trail at +1R.
        </p>
        </>
      ) : (
        <NseView />
      )}
      </main>
    </div>
  );
}

function StatCard({ label, value, accent, customColor, footnote }: {
  label: string; value: string; accent?: string; customColor?: string;
  footnote?: string;
}) {
  const color = customColor ? "" :
                accent === "green" ? "text-green-400" :
                accent === "red"   ? "text-red-400" :
                "text-white";
  return (
    <div className="border border-[#1e1e30] rounded-lg px-3 sm:px-4 py-2.5 sm:py-3 bg-[#0e0e1a] min-w-0">
      <p className="text-[11px] sm:text-xs text-gray-500 truncate">{label}</p>
      <p className={`text-base sm:text-lg font-semibold ${color} mt-1 truncate`}
         style={customColor ? { color: customColor } : undefined}
         title={value}>
        {value}
      </p>
      {footnote && (
        <p className="text-[10px] text-yellow-500/70 mt-1 leading-tight">{footnote}</p>
      )}
    </div>
  );
}


