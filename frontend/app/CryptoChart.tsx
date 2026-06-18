"use client";
import { useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Candle = { time: number; open: number; high: number; low: number; close: number };
type Zone   = { top: number; bottom: number; price: number; strength: number;
                volume?: number; volume_norm?: number };
type Level  = { price: number; type: "support" | "resistance"; strength: number };
type EmaPt  = { time: number; value: number };

type ChartData = {
  candles: Candle[];
  levels?: Level[];
  supply_zones?: Zone[];
  demand_zones?: Zone[];
  structure?: string;
  position?: string;
  current_price?: number;
  nearest_support?: number | null;
  nearest_resistance?: number | null;
  ema20?: EmaPt[];
  ema50?: EmaPt[];
  ema200?: EmaPt[];
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  error?: string;
};

type Props = {
  // Asset → live mark mapping. The chart picks the correct one based on
  // its internal asset toggle, so the LIVE line and candle extension never
  // get the WRONG asset's price.
  livePrices?: { BTC?: number | null; ETH?: number | null };
};

// Timeframe → (resolution sent to Delta, lookback hours). Tuned to keep
// the candle count visible without going off the right edge.
const TIMEFRAMES = {
  "5m":  { resolution: "5m",  hours:   24, label: "last 24h"   },
  "15m": { resolution: "15m", hours:   72, label: "last 3d"    },
  "1h":  { resolution: "1h",  hours:  168, label: "last 7d"    },
  "4h":  { resolution: "4h",  hours:  720, label: "last 30d"   },
  "1d":  { resolution: "1d",  hours: 2160, label: "last 90d"   },
} as const;
type Timeframe = keyof typeof TIMEFRAMES;
const DEFAULT_TF: Timeframe = "5m";

const STRUCTURE_COLORS: Record<string, string> = {
  uptrend:   "#22c55e",
  downtrend: "#ef4444",
  ranging:   "#94a3b8",
};

const POSITION_LABELS: Record<string, { label: string; color: string }> = {
  at_resistance: { label: "⚠ AT SUPPLY — watch for SHORT", color: "#ef4444" },
  at_support:    { label: "⚡ AT DEMAND — watch for LONG", color: "#22c55e" },
  breaking_up:   { label: "🚀 BREAKING UP", color: "#22c55e" },
  breaking_down: { label: "🔻 BREAKING DOWN", color: "#ef4444" },
  open_air:      { label: "Open air — between zones", color: "#94a3b8" },
};

export default function CryptoChart({ livePrices }: Props) {
  const [asset, setAsset] = useState<"BTC" | "ETH">("BTC");
  const [timeframe, setTimeframe] = useState<Timeframe>(DEFAULT_TF);
  const containerRef   = useRef<HTMLDivElement>(null);
  const chartRef       = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const livePriceLineRef = useRef<any>(null);
  const [data, setData] = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Pick the live price for the asset currently shown — this is the bug that
  // caused the ETH chart to render with BTC's $63k LIVE line crushing the
  // ETH candles to invisibility.
  const livePrice = livePrices?.[asset] ?? undefined;

  // Fetch on asset / timeframe change + every 30s
  useEffect(() => {
    let cancelled = false;
    const tf = TIMEFRAMES[timeframe];
    const load = (initial: boolean) => {
      if (initial) { setLoading(true); setErr(null); }
      const token = localStorage.getItem("aq_token");
      const headers = { Authorization: `Bearer ${token}` };
      fetch(`${API_URL}/api/crypto/candles?asset=${asset}&resolution=${tf.resolution}&hours=${tf.hours}`, { headers })
        .then(r => r.json())
        .then((d: ChartData) => {
          if (cancelled) return;
          if (d.error && initial) setErr(d.error);
          setData(d);
          if (initial) setLoading(false);
        })
        .catch(e => {
          if (cancelled) return;
          if (initial) { setErr(e?.message || "Network error"); setLoading(false); }
        });
    };
    load(true);
    const iv = setInterval(() => load(false), 30_000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [asset, timeframe]);

  // Build/rebuild chart when data arrives
  useEffect(() => {
    if (!data?.candles?.length || !containerRef.current) return;
    import("lightweight-charts").then(({ createChart, LineStyle, CrosshairMode }) => {
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }

      const chart = createChart(containerRef.current!, {
        width:  containerRef.current!.clientWidth,
        height: 520,
        layout: { background: { color: "#0e0e1a" }, textColor: "#94a3b8" },
        grid:   { vertLines: { color: "#1e1e30" }, horzLines: { color: "#1e1e30" } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#1e1e30" },
        timeScale: {
          borderColor: "#1e1e30",
          timeVisible: true,
          secondsVisible: false,
          // Render in the browser's LOCAL timezone (IST for India). Without
          // this, lightweight-charts defaults to UTC which is off by 5h30
          // and made the time labels look "wrong".
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000);
            const hh = d.getHours().toString().padStart(2, "0");
            const mm = d.getMinutes().toString().padStart(2, "0");
            return `${hh}:${mm}`;
          },
        },
        localization: {
          timeFormatter: (t: number) => {
            const d = new Date(t * 1000);
            return d.toLocaleString(undefined, {
              day: "2-digit", month: "short",
              hour: "2-digit", minute: "2-digit", hour12: false,
            });
          },
        },
      });
      chartRef.current = chart;

      const candles = data.candles;
      const tStart = candles[0].time;
      const tEnd   = candles[candles.length - 1].time;

      // ── Supply zones (red filled rectangles) — volume-weighted opacity ────
      (data.supply_zones || []).forEach(z => {
        const op = 0.10 + (z.volume_norm ?? 0) * 0.30;   // 0.10-0.40
        const color = `rgba(239, 68, 68, ${op})`;
        const band = chart.addBaselineSeries({
          baseValue: { type: "price", price: z.bottom },
          topFillColor1: color, topFillColor2: color,
          topLineColor: "rgba(0,0,0,0)",
          bottomFillColor1: "rgba(0,0,0,0)", bottomFillColor2: "rgba(0,0,0,0)",
          bottomLineColor: "rgba(0,0,0,0)",
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        band.setData([
          { time: tStart as any, value: z.top },
          { time: tEnd   as any, value: z.top },
        ]);
      });

      // ── Demand zones (green filled rectangles) ─────────────────────────────
      (data.demand_zones || []).forEach(z => {
        const op = 0.10 + (z.volume_norm ?? 0) * 0.30;
        const color = `rgba(34, 197, 94, ${op})`;
        const band = chart.addBaselineSeries({
          baseValue: { type: "price", price: z.bottom },
          topFillColor1: color, topFillColor2: color,
          topLineColor: "rgba(0,0,0,0)",
          bottomFillColor1: "rgba(0,0,0,0)", bottomFillColor2: "rgba(0,0,0,0)",
          bottomLineColor: "rgba(0,0,0,0)",
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        band.setData([
          { time: tStart as any, value: z.top },
          { time: tEnd   as any, value: z.top },
        ]);
      });

      // ── Candles (drawn after zones so they sit on top) ────────────────────
      const candleSeries = chart.addCandlestickSeries({
        upColor:   "#22c55e", downColor: "#ef4444",
        borderUpColor: "#22c55e", borderDownColor: "#ef4444",
        wickUpColor:   "#22c55e", wickDownColor:   "#ef4444",
      });
      candleSeries.setData(candles.map(c => ({ ...c, time: c.time as any })));
      candleSeriesRef.current = candleSeries;

      // ── Zone borders (top + bottom thin lines so the band edges are crisp)
      const lw1 = 1 as 1 | 2 | 3 | 4;
      (data.supply_zones || []).forEach((z, i) => {
        candleSeries.createPriceLine({
          price: z.top, color: "#ef4444aa", lineWidth: lw1,
          lineStyle: LineStyle.Solid, axisLabelVisible: false,
          title: i === 0 ? "SUPPLY" : "",
        });
        candleSeries.createPriceLine({
          price: z.bottom, color: "#ef4444aa", lineWidth: lw1,
          lineStyle: LineStyle.Solid, axisLabelVisible: false, title: "",
        });
      });
      (data.demand_zones || []).forEach((z, i) => {
        candleSeries.createPriceLine({
          price: z.top, color: "#22c55eaa", lineWidth: lw1,
          lineStyle: LineStyle.Solid, axisLabelVisible: false,
          title: i === 0 ? "DEMAND" : "",
        });
        candleSeries.createPriceLine({
          price: z.bottom, color: "#22c55eaa", lineWidth: lw1,
          lineStyle: LineStyle.Solid, axisLabelVisible: false, title: "",
        });
      });

      // ── S/R levels (dashed) ───────────────────────────────────────────────
      // To keep the right axis readable, we cap to the 3 strongest R + 3
      // strongest S levels and only show axis labels on the NEAREST R + S
      // to the current live price. The rest are drawn as silent dashed
      // lines on the chart — visible but not crowding the price scale.
      const livePxForLabels = livePrice ?? candles[candles.length - 1].close;
      const allLevels = data.levels || [];
      const resistances = allLevels.filter(l => l.type === "resistance").slice(0, 3);
      const supports    = allLevels.filter(l => l.type === "support").slice(0, 3);
      // nearest above + nearest below live price get axis labels
      const nearestR = resistances
        .filter(l => l.price >= livePxForLabels)
        .sort((a, b) => a.price - b.price)[0];
      const nearestS = supports
        .filter(l => l.price <= livePxForLabels)
        .sort((a, b) => b.price - a.price)[0];

      [...resistances, ...supports].forEach((lvl) => {
        const isR = lvl.type === "resistance";
        const isNearest = lvl === nearestR || lvl === nearestS;
        candleSeries.createPriceLine({
          price: lvl.price,
          color: isR ? "#ef444466" : "#22c55e66",
          lineWidth: lw1, lineStyle: LineStyle.Dashed,
          axisLabelVisible: isNearest,
          title: isNearest ? (isR ? "R" : "S") : "",
        });
      });

      // ── POC / VAH / VAL (neon blue, volume profile) ───────────────────────
      // POC gets the axis label (the heaviest-traded price). VAH/VAL are kept
      // as silent dashed lines — useful visually, redundant on the axis.
      const neon = "#00f5ff";
      if (data.poc) candleSeries.createPriceLine({
        price: data.poc, color: neon, lineWidth: lw1,
        lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "POC",
      });
      if (data.vah) candleSeries.createPriceLine({
        price: data.vah, color: neon, lineWidth: lw1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "",
      });
      if (data.val) candleSeries.createPriceLine({
        price: data.val, color: neon, lineWidth: lw1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "",
      });

      // ── EMAs ──────────────────────────────────────────────────────────────
      // Last-value axis label only on EMA200 (the trend reference). EMA20 and
      // EMA50 ride visually as colored lines without crowding the axis. If
      // any two EMAs are within 0.3% of each other (converged in chop) we
      // suppress the EMA200 label too — three converged labels stacked on
      // the same price are pure noise.
      const emasConverged = (() => {
        const last = (s?: EmaPt[]) => (s && s.length ? s[s.length - 1].value : null);
        const a = last(data.ema20), b = last(data.ema50), c = last(data.ema200);
        if (a == null || b == null || c == null) return false;
        const spread = Math.max(a, b, c) - Math.min(a, b, c);
        return spread / Math.max(a, b, c) < 0.003;
      })();
      const addEma = (pts: EmaPt[] | undefined, color: string,
                      w: 1 | 2 | 3 | 4, label: string, showAxisLabel: boolean) => {
        if (!pts?.length) return;
        const s = chart.addLineSeries({
          color, lineWidth: w,
          priceLineVisible: false, lastValueVisible: showAxisLabel,
          title: showAxisLabel ? label : "",
          crosshairMarkerVisible: false,
        });
        s.setData(pts.map(p => ({ ...p, time: p.time as any })));
      };
      addEma(data.ema20,  "#f59e0b", 1, "EMA20",  false);
      addEma(data.ema50,  "#3b82f6", 1, "EMA50",  false);
      addEma(data.ema200, "#a855f7", 2, "EMA200", !emasConverged);

      // ── Live price line ───────────────────────────────────────────────────
      livePriceLineRef.current = candleSeries.createPriceLine({
        price: livePrice ?? candles[candles.length - 1].close,
        color: asset === "BTC" ? "#f7931a" : "#627eea",
        lineWidth: 2 as 1 | 2 | 3 | 4,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: "LIVE",
      });

      chart.timeScale().fitContent();
      const ro = new ResizeObserver(() => {
        if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(containerRef.current!);
      return () => ro.disconnect();
    });

    return () => {
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }
    };
  }, [data, asset]);

  // Live price tick — update last candle in place
  useEffect(() => {
    if (!livePrice || !candleSeriesRef.current || !livePriceLineRef.current) return;
    livePriceLineRef.current.applyOptions({ price: livePrice });
    if (data?.candles?.length) {
      const last = data.candles[data.candles.length - 1];
      const hi = Math.max(last.high, livePrice);
      const lo = Math.min(last.low,  livePrice);
      candleSeriesRef.current.update({
        time: last.time as any,
        open: last.open, high: hi, low: lo, close: livePrice,
      });
    }
  }, [livePrice]);

  const struct = data?.structure ? STRUCTURE_COLORS[data.structure] ?? "#94a3b8" : "#94a3b8";
  const pos    = data?.position ? POSITION_LABELS[data.position] : null;
  const nSupplyZones = data?.supply_zones?.length ?? 0;
  const nDemandZones = data?.demand_zones?.length ?? 0;

  return (
    <div className="border border-[#1e1e30] rounded-2xl overflow-hidden bg-[#0e0e1a]">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1e1e30]">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-sm font-semibold">
            <span className={asset === "BTC" ? "text-[#f7931a]" : "text-[#627eea]"}>
              {asset}USD
            </span>
            <span className="text-gray-500 font-normal ml-2 text-xs">
              {timeframe} · {TIMEFRAMES[timeframe].label}
            </span>
          </h3>
          {livePrice && (
            <span className="text-xs text-gray-400 font-mono">
              ${livePrice.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            </span>
          )}
          {data?.structure && (
            <span className="text-xs font-medium px-2 py-0.5 rounded-full"
                  style={{ background: struct + "22", color: struct }}>
              {data.structure.toUpperCase()}
            </span>
          )}
          {pos && (
            <span className="text-xs font-medium" style={{ color: pos.color }}>
              {pos.label}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {/* Timeframe selector */}
          <div className="flex gap-0.5 bg-[#1e1e30]/40 rounded px-0.5 py-0.5">
            {(Object.keys(TIMEFRAMES) as Timeframe[]).map(tf => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-2 py-0.5 text-[10px] rounded font-medium transition-colors ${
                  timeframe === tf
                    ? "bg-white/10 text-white"
                    : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
          {/* Asset toggle */}
          <div className="flex gap-1">
            {(["BTC", "ETH"] as const).map(a => (
              <button
                key={a}
                onClick={() => setAsset(a)}
                className={`px-3 py-1 text-xs rounded ${
                  asset === a
                    ? a === "BTC" ? "bg-[#f7931a]/20 text-[#f7931a]" : "bg-[#627eea]/20 text-[#627eea]"
                    : "text-gray-500 hover:text-white"
                }`}
              >
                {a}
              </button>
            ))}
          </div>
        </div>
      </div>

      {loading && (
        <div className="h-[520px] flex items-center justify-center text-gray-500 text-sm">
          Loading {asset} {timeframe} chart…
        </div>
      )}
      {err && !loading && (
        <div className="h-[520px] flex items-center justify-center text-red-400 text-sm">
          {err}
        </div>
      )}
      <div ref={containerRef} className={loading || err ? "hidden" : ""} />

      {!loading && !err && data && (
        <div className="flex gap-4 px-4 py-2 border-t border-[#1e1e30] text-[10px] text-gray-500 flex-wrap">
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-3 bg-red-500/30 border border-red-500/60" />
            Supply zone × {nSupplyZones}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-3 bg-green-500/30 border border-green-500/60" />
            Demand zone × {nDemandZones}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dashed border-red-400/60" />
            R levels
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dashed border-green-400/60" />
            S levels
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t border-[#00f5ff]" />
            POC / VAH / VAL
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-amber-400" />EMA20
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-blue-400" />EMA50
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-[3px] border-purple-500" />EMA200
          </span>
          <span className="ml-auto text-gray-600">
            zone band opacity ∝ volume traded
          </span>
        </div>
      )}
    </div>
  );
}
