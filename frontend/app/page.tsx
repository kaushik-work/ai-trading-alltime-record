"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "./components/Header";
import CryptoChart from "./CryptoChart";
import NseView from "./NseView";
import PositionCard, { Position } from "./components/PositionCard";
import SignalRadar, { SignalRow } from "./components/SignalRadar";
import ActivityPanel, { MissedSignal, ShadowSummary, ShadowTrade } from "./components/ActivityPanel";
import { Banner, Card, CardHead, Pill, Stat } from "./components/ui";
import {
  API, WS, clearToken, clockTime, getToken, isTokenValid, inr, signedUsd, usd,
} from "./lib/format";

type KillResult = { ok: boolean; killed_strategies?: string[]; message?: string; error?: string };

type PortfolioState = {
  wallet_usd: number | null;
  wallet_inr?: number | null;
  wallet_pool_usd?: number | null;
  capital_use_pct?: number;
  fixed_capital_mode?: boolean;
  fixed_capital_inr?: number | null;
  day_pnl: number;
  open_positions: number;
  killed?: boolean;
  mode?: string;
};

type StreamDiag = { connected: boolean; marks_fresh?: number; marks_total?: number };

type Snapshot = {
  ts: string;
  perp_marks: Record<string, number>;
  positions?: Position[];
  shadow_trades?: ShadowTrade[];
  shadow_summary?: ShadowSummary;
  missed_signals?: MissedSignal[];
  signals: SignalRow[];
  portfolio: PortfolioState;
  stream: StreamDiag;
};

function signalTone(s: SignalRow): "up" | "down" | "warn" | "brand" | "neutral" {
  if (s.side === "buy") return "up";
  if (s.side === "sell") return "down";
  if (!s.ready) return "warn";
  return "neutral";
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
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function logoutAndLogin() {
    clearToken();
    router.replace("/login");
  }

  async function handleKill() {
    setKillBusy(true);
    setKillResult(null);
    try {
      const r = await fetch(`${API}/api/crypto/kill`, {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      setKillResult(await r.json());
    } catch (e: any) {
      setKillResult({ ok: false, error: e?.message || "Network error" });
    } finally {
      setKillBusy(false);
      setKillConfirm(false);
    }
  }

  useEffect(() => {
    if (!isTokenValid(getToken())) logoutAndLogin();
    else setAuthed(true);
  }, []);

  useEffect(() => {
    if (!authed) return;

    const connect = () => {
      const token = getToken();
      if (!isTokenValid(token)) { logoutAndLogin(); return; }
      setWsState("connecting");
      const ws = new WebSocket(`${WS}/ws/crypto?token=${encodeURIComponent(token || "")}`);
      wsRef.current = ws;
      ws.onopen = () => setWsState("open");
      ws.onmessage = (ev) => {
        try { setSnap(JSON.parse(ev.data) as Snapshot); } catch { /* malformed */ }
      };
      ws.onclose = (ev) => {
        setWsState("closed");
        wsRef.current = null;
        if (ev.code === 1008) { logoutAndLogin(); return; }  // token rejected
        reconnectTimer.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };
    connect();

    const onVisible = () => {
      if (document.visibilityState === "visible" && !isTokenValid(getToken())) logoutAndLogin();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [authed]);

  if (!authed) return null;

  const loading = snap == null;
  const signals = snap?.signals ?? [];
  const portfolio = snap?.portfolio;
  const positions = snap?.positions ?? [];
  const liveEth = snap?.perp_marks?.["ETHUSD"] ?? null;
  const eth = signals[0];

  const openPnlUsd = positions.reduce((a, p) => a + (p.unrealized_usd || 0), 0);
  const dayPnl = portfolio?.day_pnl ?? 0;

  const connection = wsState === "open" ? "live" : wsState === "connecting" ? "connecting" : "offline";

  return (
    <div className="min-h-screen bg-[var(--plane)]">
      <Header mode={viewMode} onModeChange={setViewMode} connection={connection} />

      <main className="max-w-[1400px] mx-auto px-3 sm:px-6 py-4 sm:py-6 pb-24 lg:pb-6">
        {viewMode === "nse" ? <NseView /> : (
          <>
            {/* ── Title + primary action ─────────────────────────────────── */}
            <div className="flex items-start justify-between gap-3 mb-4 sm:mb-6 flex-wrap">
              <div className="min-w-0">
                <h1 className="text-xl sm:text-2xl font-semibold text-[var(--ink)]">Crypto · Delta India</h1>
                <p className="text-xs text-[var(--ink-3)] mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span>Price-action S/R retest · ETHUSD perp</span>
                  {snap?.ts && <span className="tnum">· updated {clockTime(snap.ts)}</span>}
                  {snap?.stream && (
                    <span className="tnum">
                      · stream {snap.stream.connected ? "connected" : "down"}
                    </span>
                  )}
                </p>
              </div>
              {/* Desktop kill; mobile gets the sticky bar below */}
              <button onClick={() => setKillConfirm(true)} disabled={killBusy}
                      className="btn btn-danger hidden lg:inline-flex">
                {killBusy ? "Stopping…" : "Stop bot & close positions"}
              </button>
            </div>

            {killResult && (
              <div className="mb-4">
                <Banner tone={killResult.ok ? "down" : "warn"}
                        title={killResult.ok ? "Bot stopped" : "Could not stop the bot"}
                        body={killResult.message || killResult.error}
                        onDismiss={() => setKillResult(null)} />
              </div>
            )}

            {portfolio?.killed && (
              <div className="mb-4">
                <Banner tone="warn" title="Kill switch is active"
                        body="No new entries will be taken. Restart the API container to resume trading." />
              </div>
            )}

            {/* ── KPI row ────────────────────────────────────────────────── */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4 sm:mb-6">
              <Stat label="Today's P&L" emphasis loading={loading}
                    value={signedUsd(dayPnl)}
                    footnote="Realized, resets 00:00 UTC" />
              <Stat label="Open P&L" loading={loading}
                    value={positions.length ? signedUsd(openPnlUsd) : "—"}
                    footnote={positions.length ? `${positions.length} position${positions.length > 1 ? "s" : ""}` : "Flat"} />
              <Stat label="Tradeable pool" loading={loading}
                    value={portfolio?.wallet_pool_usd != null ? usd(portfolio.wallet_pool_usd, 0)
                          : portfolio?.mode === "paper" ? "Paper" : "—"}
                    footnote={portfolio?.fixed_capital_mode && portfolio.fixed_capital_inr
                      ? `${inr(portfolio.fixed_capital_inr)} fixed per trade`
                      : portfolio?.wallet_inr ? `incl. ${inr(portfolio.wallet_inr)}` : undefined} />
              <Stat label="Mode" loading={loading}
                    value={portfolio?.mode === "live" ? "Live" : portfolio?.mode === "paper" ? "Paper" : "—"}
                    footnote={portfolio?.killed ? "Halted by kill switch" : "Accepting entries"} />
            </div>

            {/* ── Main grid: chart + signals left, position + activity right ── */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 sm:gap-6 items-start">
              <div className="lg:col-span-8 space-y-4 sm:space-y-6 min-w-0">
                <CryptoChart
                  livePrice={liveEth}
                  signal={eth ? {
                    stateLabel: eth.side ? (eth.side === "buy" ? "LONG firing" : "SHORT firing")
                              : eth.ready ? eth.trend : "warming up",
                    tone: signalTone(eth),
                    reason: `Trend ${eth.trend}, 4h range ${eth.width_pct.toFixed(2)}%`,
                  } : null}
                />
                <SignalRadar signals={signals} loading={loading} />
              </div>

              <div className="lg:col-span-4 space-y-4 sm:space-y-6 min-w-0">
                <PositionCard positions={positions} loading={loading} />
                <ActivityPanel
                  shadowTrades={snap?.shadow_trades ?? []}
                  shadowSummary={snap?.shadow_summary}
                  missedSignals={snap?.missed_signals ?? []}
                />
                <Card>
                  <CardHead title="Strategy" right={<Pill tone="neutral">ETH only</Pill>} />
                  <div className="card-pad text-[13px] text-[var(--ink-2)] leading-relaxed space-y-2">
                    <p>
                      Enters at 4-hour support/resistance edges in the direction of the
                      24-hour trend, but only when the candle wick actually touches the
                      level and a strong reversal candle closes.
                    </p>
                    {eth && (
                      <p className="tnum">
                        Bracket: {(eth.sl_pct * 100).toFixed(2)}% stop ·{" "}
                        {(eth.tp_pct * 100).toFixed(2)}% target ·{" "}
                        1:{Math.round(eth.tp_pct / eth.sl_pct)} R:R
                      </p>
                    )}
                    <p className="text-[var(--ink-3)] text-xs">
                      Exits are a pure stop/target bracket — no trailing stop in the
                      current regime.
                    </p>
                  </div>
                </Card>
              </div>
            </div>
          </>
        )}
      </main>

      {/* ── Mobile sticky action bar ─────────────────────────────────────── */}
      {viewMode === "crypto" && (
        <div className="lg:hidden fixed bottom-0 inset-x-0 z-30 p-3 bg-[var(--surface)]/95 backdrop-blur border-t border-[var(--line)]"
             style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}>
          <button onClick={() => setKillConfirm(true)} disabled={killBusy}
                  className="btn btn-danger w-full">
            {killBusy ? "Stopping…" : "Stop bot & close positions"}
          </button>
        </div>
      )}

      {/* ── Kill confirmation ────────────────────────────────────────────── */}
      {killConfirm && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-0 sm:p-4"
             role="dialog" aria-modal="true" aria-labelledby="kill-title"
             onClick={() => setKillConfirm(false)}>
          <div className="bg-[var(--surface)] w-full sm:max-w-md rounded-t-[var(--r-lg)] sm:rounded-[var(--r-lg)] p-5 shadow-[var(--shadow-md)]"
               onClick={(e) => e.stopPropagation()}
               style={{ paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))" }}>
            <h3 id="kill-title" className="text-base font-semibold text-[var(--ink)]">
              Stop the bot and close all positions?
            </h3>
            <p className="text-sm text-[var(--ink-2)] mt-2 leading-relaxed">
              Every open crypto position is closed at market price and no new entries
              are taken until the API container restarts.
            </p>
            {positions.length > 0 && (
              <div className="mt-3 rounded-[var(--r-sm)] bg-[var(--surface-2)] p-3 text-[13px]">
                <p className="label mb-1">Will close now</p>
                {positions.map((p) => (
                  <p key={p.strategy} className="tnum text-[var(--ink-2)]">
                    {p.symbol} {p.side === "buy" ? "LONG" : "SHORT"} · {p.contracts} contracts ·{" "}
                    {signedUsd(p.unrealized_usd)}
                  </p>
                ))}
              </div>
            )}
            <div className="flex gap-2 mt-5">
              <button onClick={() => setKillConfirm(false)} className="btn btn-ghost flex-1">Cancel</button>
              <button onClick={handleKill} disabled={killBusy} className="btn btn-danger flex-1">
                {killBusy ? "Stopping…" : "Yes, stop it"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
