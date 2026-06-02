"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const _API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type KillResult = {
  ok: boolean;
  killed_strategies?: string[];
  open_after?: string[];
  kill_switch_armed?: boolean;
  message?: string;
  error?: string;
};

// Each signal row: which expiry, predicted move %, n strikes, ATM strike
type SignalRow = {
  underlying: string;     // BTC, ETH, XAUT
  spot: number;
  expiry: string;
  pred_pct: number;       // synthetic-forward deviation
  n_strikes: number;
  atm_strike: number;
  tte_hours: number;
};

type PortfolioState = {
  equity: number;
  day_pnl: number;
  open_positions: number;
  rolling_sharpe: number;
  max_dd_pct: number;
};

const GATE_PCT = 0.6;  // matches v5 tight gate

export default function CryptoHome() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [portfolio, setPortfolio] = useState<PortfolioState | null>(null);
  const [lastTick, setLastTick] = useState<string>("");
  const [killConfirm, setKillConfirm] = useState(false);
  const [killBusy, setKillBusy] = useState(false);
  const [killResult, setKillResult] = useState<KillResult | null>(null);

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

  // Poll the backend every 60 seconds for current signals + portfolio
  useEffect(() => {
    if (!authed) return;
    const fetchAll = async () => {
      try {
        const token = localStorage.getItem("aq_token");
        const headers = { Authorization: `Bearer ${token}` };
        const [sigRes, portRes] = await Promise.all([
          fetch(`${_API}/api/crypto/signals`, { headers }).catch(() => null),
          fetch(`${_API}/api/crypto/portfolio`, { headers }).catch(() => null),
        ]);
        if (sigRes?.ok) setSignals(await sigRes.json());
        if (portRes?.ok) setPortfolio(await portRes.json());
        setLastTick(new Date().toLocaleTimeString());
      } catch (e) {
        console.error(e);
      }
    };
    fetchAll();
    const id = setInterval(fetchAll, 60_000);
    return () => clearInterval(id);
  }, [authed]);

  if (!authed) return null;

  const firing = signals.filter(s => Math.abs(s.pred_pct) >= GATE_PCT);

  return (
    <div className="min-h-screen bg-[#0a0a14] text-gray-200">
      <Header
        mode="crypto"
        connected={signals.length > 0 || lastTick !== ""}
        botStatus={portfolio?.open_positions ? "running" : "idle"}
        onBotToggle={() => { /* crypto bot toggle TBD via /api/crypto/toggle */ }}
        errorCount={0}
        settings={{ min_lots: 1 }}
      />
      <main className="max-w-6xl mx-auto px-6 py-8">

        {/* ── Header bar ──────────────────────────────────────────────────── */}
        <div className="flex items-baseline justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold bg-gradient-to-r from-[#f7931a] to-[#627eea] bg-clip-text text-transparent">
              Crypto · Delta India
            </h1>
            <p className="text-xs text-gray-500 mt-1">
              v5 synthetic-forward · BTC/ETH/XAUT · {lastTick && `last update ${lastTick}`}
            </p>
          </div>
          <div className="flex items-center gap-3">
            {/* EMERGENCY KILL — always visible */}
            <button
              onClick={() => setKillConfirm(true)}
              disabled={killBusy}
              className="px-4 py-2 text-xs font-semibold text-white rounded-lg shadow-md hover:opacity-90 disabled:opacity-50"
              style={{ background: "linear-gradient(135deg,#dc2626 0%,#7f1d1d 100%)" }}
            >
              {killBusy ? "Killing..." : "🛑 KILL CRYPTO BOT"}
            </button>
            <button
              onClick={() => router.push("/")}
              className="px-4 py-2 text-xs text-gray-400 hover:text-white border border-[#1e1e30] rounded-lg"
            >
              → switch to NSE
            </button>
          </div>
        </div>

        {/* ── Kill result banner ────────────────────────────────────────── */}
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

        {/* ── Kill confirmation modal ───────────────────────────────────── */}
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

        {/* ── Portfolio ribbon ────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-8">
          <StatCard label="Equity" value={portfolio ? `$${portfolio.equity.toLocaleString()}` : "—"} />
          <StatCard label="Today P&L" value={portfolio ? `$${portfolio.day_pnl.toLocaleString()}` : "—"}
                    accent={portfolio && portfolio.day_pnl > 0 ? "green" : "red"} />
          <StatCard label="Open positions" value={portfolio ? `${portfolio.open_positions}` : "—"} />
          <StatCard label="Sharpe (20)" value={portfolio ? portfolio.rolling_sharpe.toFixed(2) : "—"} />
          <StatCard label="Max DD" value={portfolio ? `${portfolio.max_dd_pct.toFixed(1)}%` : "—"} />
        </div>

        {/* ── Signal Radar ────────────────────────────────────────────────── */}
        <div className="border border-[#1e1e30] rounded-2xl p-6 mb-8 bg-[#0e0e1a]">
          <div className="flex items-baseline justify-between mb-4">
            <h2 className="text-lg font-semibold">Signal Radar</h2>
            <span className="text-xs text-gray-500">
              gate |pred| ≥ {GATE_PCT}% · {firing.length} firing now
            </span>
          </div>

          {signals.length === 0 ? (
            <p className="text-sm text-gray-500">Loading signals from /api/crypto/signals...</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-gray-500 border-b border-[#1e1e30]">
                <tr>
                  <th className="text-left py-2">Asset</th>
                  <th className="text-right">Spot</th>
                  <th className="text-right">Expiry (UTC)</th>
                  <th className="text-right">TTE</th>
                  <th className="text-right">|pred|</th>
                  <th className="text-right">Strikes</th>
                  <th className="text-right">ATM K</th>
                  <th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => {
                  const fires = Math.abs(s.pred_pct) >= GATE_PCT;
                  return (
                    <tr key={i}
                        className={`border-b border-[#13131f] ${fires ? "bg-[#f7931a08]" : ""}`}>
                      <td className="py-2">
                        <span className={`inline-block w-2 h-2 rounded-full mr-2 ${
                          fires ? "bg-[#f7931a]" : "bg-gray-700"
                        }`} />
                        {s.underlying}
                      </td>
                      <td className="text-right">${s.spot.toLocaleString()}</td>
                      <td className="text-right text-gray-400">{s.expiry}</td>
                      <td className="text-right">{s.tte_hours.toFixed(1)}h</td>
                      <td className={`text-right font-mono ${
                        fires ? "text-[#f7931a] font-semibold" : ""
                      }`}>
                        {s.pred_pct > 0 ? "+" : ""}{s.pred_pct.toFixed(3)}%
                      </td>
                      <td className="text-right">{s.n_strikes}</td>
                      <td className="text-right">${s.atm_strike.toLocaleString()}</td>
                      <td className="text-right">
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
          )}
        </div>

        {/* ── Footnote ────────────────────────────────────────────────────── */}
        <p className="text-xs text-gray-500">
          Signal source: synthetic-forward (C − P + K vs spot) on Delta India options
          chain. v5 production strategy. Risk controls: 1.5% stop / partial TP at 1% /
          trail after 0.5% peak.
        </p>
      </main>
    </div>
  );
}

function StatCard({ label, value, accent }: { label: string; value: string; accent?: string }) {
  const color = accent === "green" ? "text-green-400"
              : accent === "red"   ? "text-red-400"
              : "text-white";
  return (
    <div className="border border-[#1e1e30] rounded-lg px-4 py-3 bg-[#0e0e1a]">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-lg font-semibold ${color} mt-1`}>{value}</p>
    </div>
  );
}
