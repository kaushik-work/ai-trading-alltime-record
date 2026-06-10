"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "./components/Header";
import CryptoChart from "./CryptoChart";

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
  expiry: string;
  pred_pct: number;
  n_strikes: number;
  atm_strike: number;
  tte_hours: number;
};

type PortfolioState = {
  wallet_usd: number | null;
  wallet_inr?: number | null;
  wallet_pool_usd?: number | null;   // total tradeable pool (USD + INR-converted)
  capital_use_pct?: number;           // fraction of pool deployed per cycle
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

type FuturesStat = {
  funding_rate?: number | null;
  open_interest?: number | null;
  open_interest_usd?: number | null;
  mark_price?: number | null;
  volume_24h_usd?: number | null;
  mark_change_24h?: number | null;
};

type ShadowTrade = {
  id: string;
  entry_ts: string;
  strategy: string;
  symbol: string;
  side: string;
  entry_px: number;
  pred_pct: number;
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

type Snapshot = {
  ts: string;
  perp_marks: Record<string, number>;
  futures_stats?: Record<string, FuturesStat>;
  shadow_trades?: ShadowTrade[];
  shadow_summary?: ShadowSummary;
  signals: SignalRow[];
  portfolio: PortfolioState;
  stream: StreamDiag;
};

const GATE_PCT = 0.6;

export default function CryptoHome() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
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
    if (!localStorage.getItem("aq_token")) {
      router.replace("/login");
    } else {
      setAuthed(true);
    }
  }, []);

  // WebSocket subscription — replaces the old 60s polling
  useEffect(() => {
    if (!authed) return;
    const token = localStorage.getItem("aq_token");

    const connect = () => {
      setWsState("connecting");
      const ws = new WebSocket(`${_WS}/ws/crypto?token=${encodeURIComponent(token || "")}`);
      wsRef.current = ws;
      ws.onopen = () => setWsState("open");
      ws.onmessage = (ev) => {
        try { setSnap(JSON.parse(ev.data) as Snapshot); }
        catch (e) { /* malformed payload */ }
      };
      ws.onclose = () => {
        setWsState("closed");
        wsRef.current = null;
        // Reconnect after 3s
        reconnectTimer.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, [authed]);

  if (!authed) return null;

  const signals = snap?.signals ?? [];
  const portfolio = snap?.portfolio;
  const stream = snap?.stream;
  const firing = signals.filter(s => Math.abs(s.pred_pct) >= GATE_PCT);
  const liveBtc = snap?.perp_marks?.["BTCUSD"];
  const liveEth = snap?.perp_marks?.["ETHUSD"];
  const btcFutures = snap?.futures_stats?.["BTCUSD"];
  const ethFutures = snap?.futures_stats?.["ETHUSD"];
  const shadowTrades = snap?.shadow_trades ?? [];
  const shadowSummary = snap?.shadow_summary;
  const lastShadow = shadowTrades.length ? shadowTrades[shadowTrades.length - 1] : null;

  // Max signal strength across current expiries — used for the strip stat
  const maxAbsPred = signals.length
    ? Math.max(...signals.map(s => Math.abs(s.pred_pct)))
    : 0;

  const fmtUsd = (v?: number | null) => {
    if (v == null) return "—";
    if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
    if (Math.abs(v) >= 1_000)     return `$${(v / 1_000).toFixed(1)}K`;
    return `$${v.toFixed(0)}`;
  };
  const fmtFunding = (fr?: number | null) => {
    if (fr == null) return { text: "—", color: "#475569", hint: "" };
    // Delta India returns funding_rate already in PERCENT units (e.g. 0.0074
    // = 0.0074% per 8h). We were multiplying by 100 again and showing
    // 0.7427% instead of 0.0074%. Same trap with mark_change_24h.
    const sign = fr >= 0 ? "+" : "";
    const text = `${sign}${fr.toFixed(4)}%`;
    // Positive funding -> longs paying shorts -> heavy-long, mean-revert risk
    // Negative funding -> shorts paying longs -> heavy-short, squeeze risk
    const color = fr > 0.005  ? "#ef4444" : fr < -0.005 ? "#22c55e" : "#94a3b8";
    const hint  = fr > 0.005  ? "longs paying" : fr < -0.005 ? "shorts paying" : "neutral";
    return { text, color, hint };
  };

  return (
    <div className="min-h-screen bg-[#0a0a14] text-gray-200">
      <Header
        mode="crypto"
        connected={wsState === "open"}
        botStatus={portfolio?.open_positions ? "running" : "idle"}
        onBotToggle={() => { /* crypto bot toggle TBD via /api/crypto/toggle */ }}
        errorCount={0}
        settings={{ min_lots: 1 }}
      />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">

        {/* Header bar — stacks on mobile, side-by-side on tablet+ */}
        <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-3 mb-6">
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold bg-gradient-to-r from-[#f7931a] to-[#627eea] bg-clip-text text-transparent">
              Crypto · Delta India
            </h1>
            <p className="text-xs text-gray-500 mt-1">
              v5 synthetic-forward · BTC/ETH · {snap?.ts && `last tick ${new Date(snap.ts).toLocaleTimeString()}`}
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
              portfolio?.wallet_inr && portfolio.wallet_inr > 0
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
          <StatCard label="Max |pred|" value={`${maxAbsPred.toFixed(3)}%`} />
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
                {" · pred "}{lastShadow.pred_pct >= 0 ? "+" : ""}{lastShadow.pred_pct.toFixed(3)}%
                {" · size "}{lastShadow.size_mult.toFixed(1)}×
                {lastShadow.status === "closed"
                  ? ` → closed ${lastShadow.exit_reason} ${(lastShadow.pnl_pct ?? 0) >= 0 ? "+" : ""}${(lastShadow.pnl_pct ?? 0).toFixed(2)}%`
                  : " (open)"}
              </p>
            )}
            <p className="text-[10px] text-gray-600 mt-1">
              Fund Delta wallet with USDT to convert these into live orders. Same v5 exit logic applied: 1.5% stop / trail 0.25%.
            </p>
          </div>
        )}

        {/* Futures market stats — perp-specific signals not in NIFTY land */}
        <div className="border border-[#1e1e30] rounded-2xl p-4 mb-6 bg-[#0e0e1a]">
          <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1 mb-3">
            <h2 className="text-sm font-semibold text-gray-300">Futures · Perp Stats</h2>
            <span className="text-[10px] text-gray-600 leading-tight">
              funding: + longs paying · − shorts paying
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 md:gap-4">
            <FuturesCard label="BTCUSD" futures={btcFutures} color="#f7931a"
                         fmtUsd={fmtUsd} fmtFunding={fmtFunding} />
            <FuturesCard label="ETHUSD" futures={ethFutures} color="#627eea"
                         fmtUsd={fmtUsd} fmtFunding={fmtFunding} />
          </div>
        </div>

        {/* Live BTC/ETH chart — Signal Radar below covers the pred% per expiry */}
        <div className="mb-6">
          <CryptoChart livePrices={{ BTC: liveBtc, ETH: liveEth }} />
        </div>

        {/* Signal Radar */}
        <div className="border border-[#1e1e30] rounded-2xl p-6 mb-8 bg-[#0e0e1a]">
          <div className="flex items-baseline justify-between mb-4">
            <h2 className="text-lg font-semibold">Signal Radar</h2>
            <span className="text-xs text-gray-500">
              gate |pred| ≥ {GATE_PCT}% · {firing.length} firing now
            </span>
          </div>

          {signals.length === 0 ? (
            <p className="text-sm text-gray-500">
              {wsState === "open" ? "No signals yet — stream warming up." : "Connecting…"}
            </p>
          ) : (
            <div className="overflow-x-auto -mx-2 sm:mx-0">
            <table className="w-full text-xs sm:text-sm min-w-[560px]">
              <thead className="text-gray-500 border-b border-[#1e1e30]">
                <tr>
                  <th className="text-left py-2 px-2">Asset</th>
                  <th className="text-right px-2">Spot</th>
                  <th className="text-right px-2">Expiry</th>
                  <th className="text-right px-2">TTE</th>
                  <th className="text-right px-2">|pred|</th>
                  <th className="text-right px-2 hidden sm:table-cell">Strikes</th>
                  <th className="text-right px-2 hidden sm:table-cell">ATM K</th>
                  <th className="text-right px-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => {
                  const fires = Math.abs(s.pred_pct) >= GATE_PCT;
                  return (
                    <tr key={i}
                        className={`border-b border-[#13131f] ${fires ? "bg-[#f7931a08]" : ""}`}>
                      <td className="py-2 px-2">
                        <span className={`inline-block w-2 h-2 rounded-full mr-2 ${
                          fires ? "bg-[#f7931a]" : "bg-gray-700"
                        }`} />
                        {s.underlying}
                      </td>
                      <td className="text-right px-2">${s.spot.toLocaleString()}</td>
                      <td className="text-right px-2 text-gray-400">{s.expiry}</td>
                      <td className="text-right px-2">{s.tte_hours.toFixed(1)}h</td>
                      <td className={`text-right px-2 font-mono ${
                        fires ? "text-[#f7931a] font-semibold" : ""
                      }`}>
                        {s.pred_pct > 0 ? "+" : ""}{s.pred_pct.toFixed(3)}%
                      </td>
                      <td className="text-right px-2 hidden sm:table-cell">{s.n_strikes}</td>
                      <td className="text-right px-2 hidden sm:table-cell">${s.atm_strike.toLocaleString()}</td>
                      <td className="text-right px-2">
                        {fires ? (
                          <span className={s.pred_pct > 0 ? "text-green-400" : "text-red-400"}>
                            {s.pred_pct > 0 ? "LONG" : "SHORT"}
                          </span>
                        ) : (
                          <span className="text-gray-600">flat</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          )}
        </div>

        <p className="text-xs text-gray-500">
          Signal source: synthetic-forward (C − P + K vs spot) on Delta India options
          chain. v5 production strategy. Risk controls: 1.5% stop / partial TP at 1% /
          trail after 0.5% peak.
        </p>
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

function FuturesCard({
  label, futures, color, fmtUsd, fmtFunding,
}: {
  label: string;
  futures?: FuturesStat;
  color: string;
  fmtUsd: (v?: number | null) => string;
  fmtFunding: (fr?: number | null) => { text: string; color: string; hint: string };
}) {
  const fund = fmtFunding(futures?.funding_rate);
  // Delta returns mark_change_24h already in PERCENT units (e.g. -3.14 for
  // -3.14%). We were multiplying by 100 again and showing -316.57%.
  const chgPct = futures?.mark_change_24h ?? null;
  return (
    <div className="border border-[#1e1e30] rounded-lg p-3 sm:p-4">
      <div className="flex items-baseline justify-between mb-2 gap-2">
        <span className="text-sm font-semibold flex-shrink-0" style={{ color }}>{label}</span>
        <span className="text-[11px] sm:text-xs text-gray-500 font-mono truncate">
          {futures?.mark_price != null
            ? `$${futures.mark_price.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
            : "—"}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2 sm:gap-3 text-[11px] sm:text-xs">
        <div className="min-w-0">
          <p className="text-gray-500 mb-0.5 leading-tight">
            Funding<span className="hidden sm:inline text-gray-700"> (per 8h)</span>
          </p>
          <p className="font-semibold font-mono truncate" style={{ color: fund.color }}>{fund.text}</p>
          <p className="text-[10px] text-gray-600 mt-0.5 truncate">{fund.hint}</p>
        </div>
        <div className="min-w-0">
          <p className="text-gray-500 mb-0.5 leading-tight">Open Int.</p>
          <p className="font-semibold text-white font-mono truncate">{fmtUsd(futures?.open_interest_usd)}</p>
        </div>
        <div className="min-w-0">
          <p className="text-gray-500 mb-0.5 leading-tight">24h Δ</p>
          <p className={`font-semibold font-mono truncate ${
            chgPct == null ? "text-white"
              : chgPct > 0 ? "text-green-400" : "text-red-400"
          }`}>
            {chgPct == null ? "—" : `${chgPct >= 0 ? "+" : ""}${chgPct.toFixed(2)}%`}
          </p>
        </div>
      </div>
    </div>
  );
}
