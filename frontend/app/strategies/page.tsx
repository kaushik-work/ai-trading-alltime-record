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
  {
    id: "of-ict",
    name: "ICT — Order Blocks + Sweep",
    tag: "C",
    color: "#7c3aed",
    bg: "#f5f3ff",
    tagline: "Delta + TL + ICT Confluence",
    timeframe: "5m",
    rr: "1 : 2.5",
    maxTrades: 2,
    window: "9:45–15:10 (no lunch 12:30–13:30)",
    threshold: 2,
    description: "The strongest strategy — combines Delta Direction, Trendline Channel (DP Sir HPS-T), and ICT concepts (Order Blocks + Liquidity Sweeps). Backtest: +365% in 90 days, 52.8% WR, 6.7% max DD. All 4 months profitable with ≤11% drawdown.",
    howItWorks: [
      { icon: "🏦", title: "Order Block (OB)", desc: "Last bearish candle before a bullish impulse = Bullish OB. When price retests that zone → institutions defending their long position → +1." },
      { icon: "🎯", title: "Liquidity Sweep (SSL/BSL)", desc: "Price wicks below prior swing low then closes back above it = SSL sweep. Retail stop-losses were hunted, institutions are now buying → +1." },
      { icon: "📊", title: "Delta Direction (inherited)", desc: "Session delta + dynamic delta both agree direction → ±2 score. Ensures the full session order flow confirms before ICT signals score." },
      { icon: "📐", title: "Trendline Channel (HPS-T)", desc: "DP Sir's diagonal trendlines through swing pivots. Price at lower TL (HPS) = rising support +2. Price at upper TL (HRS) = falling resistance -2." },
      { icon: "✅", title: "Triple Confluence Required", desc: "Delta alone won't trigger. ICT alone won't trigger. All three layers pointing the same direction combined with ATR base signals → score ≥6 → entry." },
      { icon: "🤖", title: "Claude AI Final Gate", desc: "Raw 5m candles, delta values, OB zone, and trendline levels sent to Claude. AI confirms or vetoes — last quality gate before order placement." },
    ],
    scoring: [
      { label: "SSL sweep (stop hunt → bullish reversal)", pts: "+1" },
      { label: "BSL sweep (stop hunt → bearish reversal)", pts: "-1" },
      { label: "Bullish Order Block retest", pts: "+1" },
      { label: "Bearish Order Block retest", pts: "-1" },
      { label: "Delta direction (session + dynamic agree)", pts: "±2" },
      { label: "Trendline channel HPS-T (support/resistance)", pts: "±2" },
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
                <span className="text-2xl font-bold px-2.5 py-1 rounded-lg font-mono" style={{ background: strat.bg, color: strat.color }}>
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
              {strat.id === "atr"    && <AtrDiagram />}
              {strat.id === "of-ict" && <IctDiagram />}
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

/* ── Strategy C — ICT Order Block + Liquidity Sweep diagram ─────────────── */
function IctDiagram() {
  return (
    <div>
      <svg viewBox="0 0 280 160" className="w-full">
        <rect width="280" height="160" rx="8" fill="#f5f3ff" />

        {/* Swing low level (liquidity pool) */}
        <line x1="10" y1="120" x2="270" y2="120" stroke="#7c3aed" strokeWidth="1" strokeDasharray="4 2" />
        <text x="212" y="118" fontSize="7" fill="#7c3aed">Swing Low</text>

        {/* Order Block zone (last red candle before up move) */}
        <rect x="58" y="90" width="16" height="30" fill="#7c3aed" fillOpacity="0.15" rx="2" />
        <text x="28" y="88" fontSize="6" fill="#7c3aed">OB zone</text>
        <line x1="28" y1="90" x2="58" y2="96" stroke="#7c3aed" strokeWidth="0.8" strokeDasharray="2 2" />

        {/* Candles — normal price action */}
        {[
          { x: 20,  o: 95, c: 100, h: 92,  l: 103, bull: true  },
          { x: 40,  o: 99, c: 96,  h: 94,  l: 102, bull: false },
          { x: 60,  o: 97, c: 92,  h: 90,  l: 100, bull: false }, // OB candle (red)
          { x: 80,  o: 91, c: 88,  h: 86,  l: 93,  bull: false }, // sweep wick below swing low
          { x: 100, o: 89, c: 95,  h: 86,  l: 97,  bull: true  }, // close back above → SSL
          { x: 120, o: 94, c: 100, h: 92,  l: 102, bull: true  },
          { x: 140, o: 99, c: 105, h: 97,  l: 107, bull: true  },
          { x: 160, o: 104, c: 110, h: 102, l: 112, bull: true },
        ].map((c, i) => {
          const color = c.bull ? "#10b981" : "#ef4444";
          const top = Math.min(c.o, c.c); const bh = Math.max(Math.abs(c.o - c.c), 2);
          return (
            <g key={i}>
              <line x1={c.x+7} y1={c.h} x2={c.x+7} y2={c.l} stroke={color} strokeWidth="1" />
              <rect x={c.x+3} y={top} width="8" height={bh} fill={color} rx="1" />
            </g>
          );
        })}

        {/* SSL sweep annotation */}
        <line x1="84" y1="86" x2="84" y2="80" stroke="#ef4444" strokeWidth="1" />
        <text x="88" y="79" fontSize="6" fill="#ef4444">SSL sweep ↓</text>
        <line x1="104" y1="86" x2="104" y2="80" stroke="#10b981" strokeWidth="1.5" />
        <text x="108" y="79" fontSize="6" fill="#10b981">Close above →</text>

        {/* Entry arrow */}
        <line x1="122" y1="92" x2="122" y2="75" stroke="#7c3aed" strokeWidth="2" />
        <polygon points="122,70 118,78 126,78" fill="#7c3aed" />
        <text x="128" y="75" fontSize="7" fill="#7c3aed" fontWeight="bold">BUY</text>

        {/* Score box */}
        <rect x="195" y="95" width="78" height="54" rx="4" fill="#fff" stroke="#ede9fe" />
        <text x="203" y="107" fontSize="6" fill="#6b7280">ICT Score</text>
        <text x="203" y="118" fontSize="7" fill="#7c3aed">SSL sweep  +1</text>
        <text x="203" y="128" fontSize="7" fill="#7c3aed">OB retest  +1</text>
        <text x="203" y="138" fontSize="7" fill="#7c3aed">Delta       +2</text>
        <text x="203" y="148" fontSize="7" fill="#7c3aed">TL (HPS-T) +2</text>
      </svg>
      <p className="text-[10px] text-gray-400 text-center mt-1">SSL sweep + OB retest + delta + trendline → entry signal</p>
    </div>
  );
}

/* ── (removed) Delta diagram — Strategy A retired ────────────────────────── */
function DeltaDiagram() {
  return (
    <div>
      <svg viewBox="0 0 280 160" className="w-full">
        <rect width="280" height="160" rx="8" fill="#f0fdf4" />

        {/* Delta bars — green positive, red negative */}
        {[
          { x: 15,  h: 30, pos: true  },
          { x: 35,  h: 45, pos: true  },
          { x: 55,  h: 20, pos: true  },
          { x: 75,  h: 10, pos: false },
          { x: 95,  h: 35, pos: true  },
          { x: 115, h: 50, pos: true  },
          { x: 135, h: 42, pos: true  },
          { x: 155, h: 38, pos: true  },
        ].map((b, i) => (
          <rect key={i} x={b.x} y={110 - b.h} width="16" height={b.h}
            fill={b.pos ? "#10b981" : "#ef4444"} fillOpacity="0.8" rx="2" />
        ))}

        {/* Zero line */}
        <line x1="10" y1="110" x2="190" y2="110" stroke="#6b7280" strokeWidth="1" />
        <text x="12" y="108" fontSize="7" fill="#6b7280">0</text>

        {/* Cum delta line */}
        <polyline points="23,100 43,80 63,68 83,72 103,55 123,35 143,28 163,22"
          fill="none" stroke="#059669" strokeWidth="2" />
        <text x="165" y="22" fontSize="7" fill="#059669">cum δ</text>

        {/* Signal arrow */}
        <line x1="115" y1="30" x2="115" y2="15" stroke="#059669" strokeWidth="2" />
        <polygon points="115,10 111,18 119,18" fill="#059669" />
        <text x="122" y="14" fontSize="7" fill="#059669">BUY</text>

        {/* Labels */}
        <text x="12" y="145" fontSize="7" fill="#6b7280">Session delta positive →</text>
        <text x="12" y="155" fontSize="7" fill="#059669">Dynamic delta confirms → ENTRY</text>

        {/* Score box */}
        <rect x="200" y="10" width="72" height="36" rx="4" fill="#fff" stroke="#d1fae5" />
        <text x="208" y="22" fontSize="7" fill="#6b7280">Delta Score</text>
        <text x="208" y="34" fontSize="10" fontWeight="bold" fill="#059669">+1 signal</text>
        <text x="208" y="44" fontSize="6" fill="#6b7280">+strategy B = +3</text>
      </svg>
      <p className="text-[10px] text-gray-400 text-center mt-1">Session + dynamic delta both positive → entry signal</p>
    </div>
  );
}

/* ── Strategy B — Trendline channel diagram ──────────────────────────────── */
function TrendlineDiagram() {
  return (
    <div>
      <svg viewBox="0 0 280 160" className="w-full">
        <rect width="280" height="160" rx="8" fill="#fffbeb" />

        {/* Upper trendline (resistance / HRS) */}
        <line x1="20" y1="40" x2="220" y2="60" stroke="#ef4444" strokeWidth="1.5" strokeDasharray="5 2" />
        <text x="222" y="62" fontSize="7" fill="#ef4444">HRS</text>

        {/* Lower trendline (support / HPS) */}
        <line x1="20" y1="100" x2="220" y2="115" stroke="#059669" strokeWidth="1.5" strokeDasharray="5 2" />
        <text x="222" y="118" fontSize="7" fill="#059669">HPS-T</text>

        {/* Channel fill */}
        <polygon points="20,40 220,60 220,115 20,100" fill="#d97706" fillOpacity="0.05" />

        {/* Price candles */}
        {[
          { x: 30,  y: 85 }, { x: 55, y: 80 }, { x: 80, y: 90 },
          { x: 105, y: 75 }, { x: 130, y: 85 }, { x: 155, y: 108 },
          { x: 180, y: 112 },
        ].map((c, i) => (
          <g key={i}>
            <line x1={c.x+4} y1={c.y-8} x2={c.x+4} y2={c.y+8} stroke={i > 4 ? "#10b981" : "#6b7280"} strokeWidth="1" />
            <rect x={c.x} y={c.y-4} width="8" height="7"
              fill={i > 4 ? "#10b981" : "#9ca3af"} rx="1" />
          </g>
        ))}

        {/* Entry arrow at HPS touch */}
        <line x1="180" y1="108" x2="180" y2="88" stroke="#059669" strokeWidth="2" />
        <polygon points="180,83 176,91 184,91" fill="#059669" />
        <text x="186" y="90" fontSize="7" fill="#059669">BUY</text>
        <text x="186" y="100" fontSize="6" fill="#059669">at HPS-T</text>

        {/* Pivot dots */}
        {[[35,95],[85,88],[155,108]].map(([x,y],i) => (
          <circle key={i} cx={x} cy={y} r="3" fill="#059669" fillOpacity="0.6" />
        ))}
        {[[40,42],[110,50],[170,56]].map(([x,y],i) => (
          <circle key={i} cx={x} cy={y} r="3" fill="#ef4444" fillOpacity="0.6" />
        ))}

        <text x="12" y="150" fontSize="7" fill="#6b7280">Lower TL = rising support (HPS-T) · Upper TL = falling resistance (HRS)</text>
      </svg>
      <p className="text-[10px] text-gray-400 text-center mt-1">Price touches lower TL → HPS-T buy zone → entry with delta confirm</p>
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
