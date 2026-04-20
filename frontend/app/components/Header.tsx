"use client";
import { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Props {
  mode: string;
  connected: boolean;
  botStatus: string;
  onBotToggle: () => void;
  errorCount?: number;
}

export default function Header({ mode, connected, botStatus, onBotToggle, errorCount = 0 }: Props) {
  const router = useRouter();
  const [menuOpen, setMenuOpen]     = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const [lots, setLots]             = useState(1);

  useEffect(() => {
    const token = localStorage.getItem("aq_token");
    if (!token) return;
    fetch(`${API_URL}/api/settings`, { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.min_lots) setLots(d.min_lots); })
      .catch(() => {});
  }, []);

  async function saveLots(n: number) {
    setLots(n);
    const token = localStorage.getItem("aq_token");
    await fetch(`${API_URL}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ min_lots: n }),
    });
  }

  // Angel One session modal
  const [tokenModal, setTokenModal] = useState(false);
  const [tokenSaving, setTokenSaving] = useState(false);
  const [tokenMsg, setTokenMsg]     = useState<{ok: boolean; text: string} | null>(null);

  function openTokenModal() {
    setMenuOpen(false);
    setTokenModal(true);
    setTokenMsg(null);
  }

  async function generateSession() {
    setTokenSaving(true);
    setTokenMsg(null);
    try {
      const res = await fetch(`${API_URL}/api/angel/session`, {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("aq_token")}` },
      });
      const j = await res.json();
      if (res.ok) {
        setTokenMsg({ ok: true, text: `Session active — set at ${j.set_at ?? "now"}` });
        setTimeout(() => { setTokenModal(false); setTokenMsg(null); }, 2000);
      } else {
        setTokenMsg({ ok: false, text: j.detail ?? "Session creation failed" });
      }
    } catch (e: any) {
      setTokenMsg({ ok: false, text: e.message ?? "Network error" });
    } finally {
      setTokenSaving(false);
    }
  }

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  function logout() {
    localStorage.removeItem("aq_token");
    router.push("/login");
  }

  async function pause() {
    const token = localStorage.getItem("aq_token");
    await fetch(`${API_URL}/api/bot/pause`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    setMenuOpen(false);
    onBotToggle();
  }

  async function resume() {
    const token = localStorage.getItem("aq_token");
    await fetch(`${API_URL}/api/bot/resume`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    setMenuOpen(false);
    onBotToggle();
  }

  return (
    <>
    <header className="w-full bg-white border-b border-gray-200 px-4 md:px-6 py-2 flex items-center justify-between shadow-sm">

      {/* Left — Logo */}
      <div className="flex items-center gap-3 cursor-pointer" onClick={() => router.push("/")}>
        <img src="/tgc-logo-svg.svg" alt="Logo" className="h-10 md:h-16 w-auto" />
      </div>

      {/* Right — actions */}
      <div className="flex items-center gap-2 md:gap-3">

        {/* Lots dropdown */}
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] font-bold text-gray-400 hidden sm:inline">LOTS</span>
          <select
            value={lots}
            onChange={e => saveLots(Number(e.target.value))}
            className="text-xs font-bold border border-gray-200 rounded-lg px-2 py-1.5 bg-white text-gray-700 cursor-pointer focus:outline-none focus:border-indigo-400"
          >
            {[1, 2, 3, 4, 5].map(n => (
              <option key={n} value={n}>{n} {n === 1 ? "Lot" : "Lots"}</option>
            ))}
          </select>
        </div>

        {/* Session — always visible */}
        <button
          onClick={openTokenModal}
          className="text-sm font-semibold px-3 md:px-4 py-2 rounded-lg transition-colors"
          style={{ background: "#eff6ff", color: "#2563eb", border: "1px solid #bfdbfe" }}
          title="Generate Angel One session (auto-TOTP)"
        >
          <span className="hidden sm:inline">Session</span>
          <span className="sm:hidden">🔑</span>
        </button>

        {/* Signal Radar — hidden on mobile (in dropdown instead) */}
        <button
          onClick={() => router.push("/debug")}
          className="hidden md:block text-sm font-semibold px-4 py-2 border border-gray-300 hover:border-gray-400 text-gray-700 rounded-lg transition-colors bg-white"
        >
          Signal Radar
        </button>

        {/* PPnL — hidden on mobile */}
        <button
          onClick={() => router.push("/pnl")}
          className="hidden md:block text-sm font-semibold px-4 py-2 border border-gray-300 hover:border-gray-400 text-gray-700 rounded-lg transition-colors bg-white"
        >
          PPnL
        </button>

        {/* BrainFry dropdown */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(o => !o)}
            className="flex items-center gap-1.5 text-sm font-semibold px-3 md:px-4 py-2 border border-gray-300 hover:border-gray-400 text-gray-700 rounded-lg transition-colors bg-white"
          >
            <span className="hidden sm:inline">BrainFry</span>
            <span className="sm:hidden">☰</span>
            <svg className={`w-3.5 h-3.5 transition-transform hidden sm:block ${menuOpen ? "rotate-180" : ""}`}
                 fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {menuOpen && (
            <div className="absolute right-0 top-full mt-2 w-52 bg-white border border-gray-200 rounded-xl shadow-xl z-50">
              {/* Bot controls */}
              <div className="px-3 py-2 text-[10px] text-gray-400 uppercase tracking-widest font-semibold">Bot</div>
              <button onClick={pause} disabled={botStatus === "paused"}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40 flex items-center gap-2">
                <span>⏸</span> Pause Bot
              </button>
              <button onClick={resume} disabled={botStatus === "running"}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40 flex items-center gap-2">
                <span>▶</span> Resume Bot
              </button>

              <div className="border-t border-gray-100 mx-3" />

              {/* Tools — always visible here; Signal Radar + PPnL also shown on mobile */}
              <div className="px-3 py-2 text-[10px] text-gray-400 uppercase tracking-widest font-semibold">Tools</div>
              <button onClick={() => { router.push("/debug"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📡</span> Signal Radar
              </button>
              <button onClick={() => { router.push("/pnl"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>💰</span> PPnL
              </button>
              <button onClick={() => { router.push("/strategies"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📋</span> Strategies
              </button>
              <button onClick={() => { router.push("/backtest"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📊</span> Backtest
              </button>
              <button onClick={() => { router.push("/journal"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📓</span> Journal
              </button>
              <button onClick={() => { router.push("/errors"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-red-50 flex items-center gap-2"
                style={{ color: errorCount > 0 ? "#b91c1c" : "#374151" }}>
                <span>⚠</span>
                <span>Broker Errors</span>
                {errorCount > 0 && (
                  <span className="ml-auto text-xs font-bold px-1.5 py-0.5 rounded-full"
                    style={{ background: "#fee2e2", color: "#b91c1c" }}>
                    {errorCount}
                  </span>
                )}
              </button>
              <button onClick={() => { router.push("/event-blocks"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📅</span> Event Blocks
              </button>
              <button onClick={() => { router.push("/paper-comparison"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2">
                <span>📊</span> Paper Comparison
              </button>

              <div className="border-t border-gray-100 mx-3 my-1" />
              <button onClick={logout} style={{ color: "#ef4444" }}
                className="w-full text-left px-4 py-2.5 mb-1 text-sm font-semibold hover:bg-red-50 rounded-b-xl flex items-center gap-2">
                <span>🚪</span> Logout
              </button>
            </div>
          )}
        </div>
      </div>
    </header>

    {/* Angel One Session Modal — one-click TOTP login, no manual token needed */}
    {tokenModal && (
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
           style={{ background: "rgba(0,0,0,0.5)" }}
           onClick={e => { if (e.target === e.currentTarget) { setTokenModal(false); setTokenMsg(null); } }}>
        <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6 space-y-5">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-bold text-gray-900">Angel One Session</h3>
            <button onClick={() => { setTokenModal(false); setTokenMsg(null); }}
                    className="text-gray-400 hover:text-gray-600 text-xl font-bold leading-none">×</button>
          </div>

          <p className="text-sm text-gray-600">
            Generates a fresh Angel One session automatically using TOTP.
            No manual login or token needed — just click the button.
          </p>

          {tokenMsg && (
            <div className={`text-xs px-3 py-2 rounded-lg ${tokenMsg.ok ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-600 border border-red-200"}`}>
              {tokenMsg.ok ? "✓ " : "✗ "}{tokenMsg.text}
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={generateSession}
              disabled={tokenSaving}
              className="flex-1 py-2.5 rounded-xl text-sm font-bold text-white transition-colors disabled:opacity-50"
              style={{ background: "#4f46e5" }}>
              {tokenSaving ? "Connecting…" : "Generate Session"}
            </button>
            <button
              onClick={() => { setTokenModal(false); setTokenMsg(null); }}
              className="px-4 py-2.5 rounded-xl text-sm font-semibold border border-gray-200 text-gray-600 hover:border-gray-400 transition-colors">
              Cancel
            </button>
          </div>
        </div>
      </div>
    )}
    </>
  );
}
