"use client";
import { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Props {
  mode: string;
  connected: boolean;
  botStatus: string;
  onBotToggle: () => void;
}

export default function Header({ mode, connected, botStatus, onBotToggle }: Props) {
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
    <header className="w-full bg-white border-b border-gray-200 px-6 py-2 flex items-center justify-between shadow-sm">

      {/* Left — Logo */}
      <div className="flex items-center gap-3 cursor-pointer" onClick={() => router.push("/")}>
        <img src="/tgc-logo-svg.svg" alt="Logo" className="h-16 w-auto" />
      </div>

      {/* Right — actions */}
      <div className="flex items-center gap-3">

        {/* Live indicator */}
        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          <span className={`w-2 h-2 rounded-full ${connected ? "bg-green-500 animate-pulse" : "bg-red-400"}`} />
          {connected ? "Live" : "Offline"}
        </div>

        {/* Mode badge */}
        <span className={`text-xs font-semibold px-3 py-1 rounded-full ${
          mode === "live"
            ? "bg-red-100 text-red-600"
            : "bg-blue-100 text-blue-600"
        }`}>
          {mode === "live" ? "● LIVE" : "● PAPER"}
        </span>

        {/* PPnL button */}
        <button
          onClick={() => router.push("/pnl")}
          className="text-sm font-semibold px-4 py-2 border border-gray-300 hover:border-gray-400 text-gray-700 rounded-lg transition-colors bg-white"
        >
          PPnL
        </button>

        {/* Brain Fry dropdown */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(o => !o)}
            className="flex items-center gap-1.5 text-sm font-semibold px-4 py-2 border border-gray-300 hover:border-gray-400 text-gray-700 rounded-lg transition-colors bg-white"
          >
            BrainFry
            <svg className={`w-3.5 h-3.5 transition-transform ${menuOpen ? "rotate-180" : ""}`}
                 fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {menuOpen && (
            <div className="absolute right-0 top-full mt-2 w-52 bg-white border border-gray-200 rounded-xl shadow-xl z-50 overflow-visible pb-1">
              {/* Bot controls */}
              <div className="px-3 py-2 text-[10px] text-gray-400 uppercase tracking-widest font-semibold">
                Bot
              </div>
              <button
                onClick={pause}
                disabled={botStatus === "paused"}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40 flex items-center gap-2"
              >
                <span>⏸</span> Pause Bot
              </button>
              <button
                onClick={resume}
                disabled={botStatus === "running"}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40 flex items-center gap-2"
              >
                <span>▶</span> Resume Bot
              </button>

              <div className="border-t border-gray-100 mx-3" />

              {/* Backtest */}
              <div className="px-3 py-2 text-[10px] text-gray-400 uppercase tracking-widest font-semibold">
                Tools
              </div>
              <button
                onClick={() => { router.push("/backtest"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2"
              >
                <span>📊</span> Backtest
              </button>
              <button
                onClick={() => { router.push("/journal"); setMenuOpen(false); }}
                className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-2"
              >
                <span>📓</span> Journal
              </button>

              <div className="border-t border-gray-100 mx-3" />

              {/* Logout */}
              <button
                onClick={logout}
                className="w-full text-left px-4 py-2.5 text-sm text-red-500 hover:bg-red-50 flex items-center gap-2"
              >
                <span>🚪</span> Logout
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
