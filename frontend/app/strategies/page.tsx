"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const STRATEGIES = [
  {
    id: "atr",
    name: "ATR Intraday",
    tag: "旧",
    color: "#6b7280",
    bg: "#f9fafb",
    tagline: "Legacy Multi-Pattern",
    timeframe: "15m",
    rr: "1 : 2.0",
    maxTrades: 3,
    window: "9:45–15:10",
    threshold: 7,
    description: "The original strategy. Uses a broad multi-indicator confluence — VWAP, ORB (Opening Range Breakout), PDH/PDL levels, and 12 candlestick patterns. Claude AI scores signals from -10 to +10 based on all conditions.",
    howItWorks: [
      { icon: "📐", title: "ORB (Opening Range Breakout)", desc: "The high/low of the first 15-min candle sets the range. Breakout above/below is a directional signal." },
      { icon: "〰️", title: "VWAP Direction", desc: "Price above/below VWAP sets the intraday bias. Trades only taken in the VWAP direction." },
      { icon: "📏", title: "PDH / PDL", desc: "Previous Day High and Low act as support/resistance. Breakouts beyond these are strong signals." },
      { icon: "🕯️", title: "12 Candlestick Patterns", desc: "Hammer, Doji, Engulfing, Pin Bar, Morning Star, and more — each adds to the confluence score." },
      { icon: "🤖", title: "Claude AI Scoring", desc: "Claude AI evaluates all conditions together and outputs a final score from -10 to +10. ≥+7 = buy CE, ≤-7 = buy PE." },
      { icon: "📏", title: "ATR-based SL/TP", desc: "Stop loss = 1× ATR below entry. Take profit = 2× ATR above entry. Dynamic sizing based on volatility." },
    ],
    scoring: [
      { label: "VWAP alignment", pts: "±2.0" },
      { label: "ORB breakout", pts: "±2.0" },
      { label: "PDH/PDL level break", pts: "±1.5" },
      { label: "Candlestick pattern (each)", pts: "±0.5–1.0" },
      { label: "Volume confirmation", pts: "±1.0" },
      { label: "RSI confirmation", pts: "±1.0" },
      { label: "Multiple pattern confluence", pts: "±1.0" },
    ],
  },
];

export default function StrategiesPage() {
  const router = useRouter();
  const [selected, setSelected] = useState("atr");
  const strat = STRATEGIES.find(s => s.id === selected)!;

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode="paper" connected={true} botStatus="market_closed" onBotToggle={() => {}} />

      <div className="max-w-5xl mx-auto w-full p-6">
        {/* Title */}
        <div className="mb-6">
          <h1 className="text-xl font-bold text-gray-900">Strategy Playbook</h1>
          <p className="text-xs text-gray-400 mt-0.5">Deep dive into how each strategy works</p>
        </div>

        {/* Tab selector */}
        <div className="flex gap-2 mb-6">
          {STRATEGIES.map(s => (
            <button
              key={s.id}
              onClick={() => setSelected(s.id)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold border transition-all"
              style={selected === s.id
                ? { background: s.color, color: "#fff", borderColor: s.color }
                : { background: "#fff", color: "#374151", borderColor: "#e5e7eb" }
              }
            >
              <span>{s.tag}</span>
              {s.name}
            </button>
          ))}
        </div>

        {/* Main content */}
        <div className="grid grid-cols-5 gap-5">

          {/* Left — description + rules */}
          <div className="col-span-3 space-y-4">

            {/* Hero card */}
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <div className="flex items-center gap-3 mb-3">
                <span className="text-2xl font-bold px-2.5 py-1 rounded-lg" style={{ background: strat.bg, color: strat.color }}>
                  {strat.tag}
                </span>
                <div>
                  <h2 className="text-lg font-bold text-gray-900">{strat.name}</h2>
                  <span className="text-xs font-semibold px-2 py-0.5 rounded-full" style={{ background: strat.bg, color: strat.color }}>
                    {strat.tagline}
                  </span>
                </div>
              </div>
              <p className="text-sm text-gray-600 leading-relaxed">{strat.description}</p>

              {/* Stats row */}
              <div className="flex gap-3 mt-4">
                {[
                  { label: "Timeframe", val: strat.timeframe },
                  { label: "R:R", val: strat.rr },
                  { label: "Max Trades", val: `${strat.maxTrades}/day` },
                  { label: "Score Threshold", val: `≥ ${strat.threshold}` },
                ].map(({ label, val }) => (
                  <div key={label} className="flex-1 bg-gray-50 rounded-lg px-3 py-2 text-center">
                    <div className="text-[9px] text-gray-400 uppercase font-semibold">{label}</div>
                    <div className="text-xs font-bold text-gray-800 mt-0.5">{val}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* How it works */}
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h3 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-4">How It Works</h3>
              <div className="space-y-3">
                {strat.howItWorks.map((rule, i) => (
                  <div key={i} className="flex gap-3 items-start">
                    <span className="text-base mt-0.5 shrink-0">{rule.icon}</span>
                    <div>
                      <div className="text-sm font-semibold text-gray-800">{rule.title}</div>
                      <div className="text-xs text-gray-500 mt-0.5 leading-relaxed">{rule.desc}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Entry window */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h3 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-2">Entry Window</h3>
              <div className="flex items-center gap-2">
                <span className="text-sm">🕐</span>
                <span className="text-sm font-bold text-gray-800">{strat.window} IST</span>
              </div>
              <p className="text-xs text-gray-400 mt-1">Trades are only initiated within this window. Existing positions are managed and closed at EOD.</p>
            </div>
          </div>

          {/* Right — visual diagram + scoring */}
          <div className="col-span-2 space-y-4">

            {/* Visual diagram */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h3 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-3">Visual Setup</h3>
              <AtrDiagram />
            </div>

            {/* Score breakdown */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h3 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-3">Score System</h3>
              <div className="space-y-1.5">
                {strat.scoring.map((item, i) => (
                  <div key={i} className="flex items-center justify-between">
                    <span className="text-xs text-gray-600">{item.label}</span>
                    <span className="text-xs font-bold px-2 py-0.5 rounded" style={{ background: strat.bg, color: strat.color }}>
                      {item.pts}
                    </span>
                  </div>
                ))}
              </div>
              <div className="mt-3 pt-3 border-t border-gray-100 flex items-center justify-between">
                <span className="text-xs font-semibold text-gray-700">Minimum to trade</span>
                <span className="text-xs font-bold px-2 py-0.5 rounded-full text-white" style={{ background: strat.color }}>
                  ≥ {strat.threshold}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── ATR Intraday Diagram — ORB + candlestick ─────────────────────────────── */
function AtrDiagram() {
  const candles = [
    { x: 20,  o: 110, h: 105, l: 118, c: 112 },
    { x: 40,  o: 112, h: 108, l: 120, c: 115 },
    { x: 60,  o: 115, h: 109, l: 122, c: 118 }, // ORB high
    { x: 80,  o: 118, h: 114, l: 123, c: 119 },
    { x: 100, o: 119, h: 108, l: 125, c: 110 }, // breakout
    { x: 120, o: 110, h: 102, l: 118, c: 104 },
    { x: 140, o: 104, h: 96,  l: 112, c: 99  },
    { x: 160, o: 99,  h: 92,  l: 108, c: 95  },
  ];

  return (
    <div>
      <svg viewBox="0 0 280 160" className="w-full">
        <rect width="280" height="160" rx="8" fill="#f9fafb" />

        {/* ORB zone */}
        <rect x="10" y="108" width="80" height="10" fill="#6366f1" fillOpacity="0.1" />
        <line x1="10" y1="108" x2="270" y2="108" stroke="#6366f1" strokeWidth="1" strokeDasharray="4 2" />
        <line x1="10" y1="118" x2="270" y2="118" stroke="#6366f1" strokeWidth="1" strokeDasharray="4 2" />

        {/* PDH */}
        <line x1="10" y1="95" x2="270" y2="95" stroke="#f97316" strokeWidth="1" strokeDasharray="5 3" />
        <text x="215" y="93" fontSize="7" fill="#f97316">PDH</text>

        {/* VWAP */}
        <line x1="10" y1="112" x2="270" y2="112" stroke="#a855f7" strokeWidth="1" />
        <text x="215" y="110" fontSize="7" fill="#a855f7">VWAP</text>

        {/* Candles */}
        {candles.map((c, i) => {
          const bull = c.c < c.o;
          const color = bull ? "#22c55e" : "#ef4444";
          const bodyTop = Math.min(c.o, c.c);
          const bodyH = Math.abs(c.o - c.c);
          return (
            <g key={i}>
              <line x1={c.x + 7} y1={c.h} x2={c.x + 7} y2={c.l} stroke={color} strokeWidth="1" />
              <rect x={c.x + 3} y={bodyTop} width="8" height={Math.max(bodyH, 2)} fill={color} rx="1" />
            </g>
          );
        })}

        {/* Score meter */}
        <rect x="195" y="125" width="70" height="20" rx="4" fill="#fff" stroke="#e5e7eb" />
        <text x="200" y="134" fontSize="7" fill="#6b7280">Score</text>
        <rect x="218" y="129" width="40" height="8" rx="2" fill="#e5e7eb" />
        <rect x="218" y="129" width="28" height="8" rx="2" fill="#22c55e" />
        <text x="250" y="136" fontSize="6" fill="#15803d">+7</text>

        {/* Entry arrow */}
        <line x1="100" y1="105" x2="100" y2="96" stroke="#22c55e" strokeWidth="2" markerEnd="url(#arrowGreen2)" />
        <text x="108" y="93" fontSize="7" fill="#22c55e">BUY CE</text>

        {/* Labels */}
        <text x="12" y="106" fontSize="7" fill="#6366f1">ORB High</text>
        <text x="12" y="148" fontSize="7" fill="#6b7280">Opening Range →</text>

        <defs>
          <marker id="arrowGreen2" markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto">
            <path d="M0,6 L3,0 L6,6 z" fill="#22c55e" />
          </marker>
        </defs>
      </svg>
      <p className="text-[10px] text-gray-400 text-center mt-1">ORB breakout + VWAP alignment + score ≥ 7 → trade</p>
    </div>
  );
}
