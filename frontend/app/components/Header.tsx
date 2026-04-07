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
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

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
    <header className="w-full bg-white border-b border-gray-200 px-4 md:px-6 py-2 flex items-center justify-between shadow-sm">

      {/* Left — Logo */}
      <div className="flex items-center gap-3 cursor-pointer" onClick={() => router.push("/")}>
        <img src="/tgc-logo-svg.svg" alt="Logo" className="h-10 md:h-16 w-auto" />
      </div>

      {/* Right — actions */}
      <div className="flex items-center gap-2 md:gap-3">

        {/* Live indicator */}
        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          <span className={`w-2 h-2 rounded-full ${connected ? "bg-green-500 animate-pulse" : "bg-red-400"}`} />
          <span className="hidden sm:inline">{connected ? "Live" : "Offline"}</span>
        </div>

        {/* Mode badge */}
        <span className={`text-xs font-semibold px-2 md:px-3 py-1 rounded-full ${
          mode === "live" ? "bg-red-100 text-red-600" : "bg-blue-100 text-blue-600"
        }`}>
          {mode === "live" ? "● LIVE" : "● PAPER"}
        </span>

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
                <span>Zerodha Errors</span>
                {errorCount > 0 && (
                  <span className="ml-auto text-xs font-bold px-1.5 py-0.5 rounded-full"
                    style={{ background: "#fee2e2", color: "#b91c1c" }}>
                    {errorCount}
                  </span>
                )}
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
  );
}
