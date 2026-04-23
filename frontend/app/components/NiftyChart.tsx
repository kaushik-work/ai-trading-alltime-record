"use client";
import { useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Level {
  price: number;
  type: "support" | "resistance";
  strength: number;
  zone_top: number;
  zone_bot: number;
  kind: "supply" | "demand";
}

interface Zone { top: number; bottom: number; price: number; strength: number; }

interface ChartData {
  candles: { time: number; open: number; high: number; low: number; close: number }[];
  levels: Level[];
  supply_zones: Zone[];
  demand_zones: Zone[];
  structure: string;
  position: string;
  current_price: number;
  nearest_support: number | null;
  nearest_resistance: number | null;
  error?: string;
}

interface Props {
  livePrice?: number;   // fed from WebSocket snapshot
}

const POSITION_LABELS: Record<string, { label: string; color: string }> = {
  at_resistance:  { label: "AT RESISTANCE — watch for rejection", color: "#ef4444" },
  at_support:     { label: "AT SUPPORT — watch for bounce",        color: "#22c55e" },
  breaking_up:    { label: "BREAKING UP through resistance",       color: "#22c55e" },
  breaking_down:  { label: "BREAKING DOWN through support",        color: "#ef4444" },
  open_air:       { label: "Open air — between levels",            color: "#94a3b8" },
};

const STRUCTURE_COLORS: Record<string, string> = {
  uptrend:   "#22c55e",
  downtrend: "#ef4444",
  ranging:   "#94a3b8",
};

export default function NiftyChart({ livePrice }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef     = useRef<any>(null);
  const candleRef    = useRef<any>(null);
  const pricLineRef  = useRef<any>(null);
  const [data, setData]       = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);

  // Fetch chart data once on mount
  useEffect(() => {
    const token = localStorage.getItem("aq_token");
    fetch(`${API_URL}/api/chart-data`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.json())
      .then((d: ChartData) => {
        if (d.error && !d.candles?.length) { setError(d.error); setLoading(false); return; }
        setData(d);
        setLoading(false);
      })
      .catch(e => { setError(String(e)); setLoading(false); });
  }, []);

  // Build chart once data arrives
  useEffect(() => {
    if (!data || !containerRef.current) return;

    // Dynamically import so SSR doesn't break
    import("lightweight-charts").then(({ createChart, CrosshairMode, LineStyle }) => {
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }

      const chart = createChart(containerRef.current!, {
        width:  containerRef.current!.clientWidth,
        height: 420,
        layout: { background: { color: "#0f172a" }, textColor: "#94a3b8" },
        grid:   { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#1e293b" },
        timeScale: {
          borderColor: "#1e293b",
          timeVisible: true,
          secondsVisible: false,
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000);
            return `${d.getHours().toString().padStart(2,"0")}:${d.getMinutes().toString().padStart(2,"0")}`;
          },
        },
      });

      chartRef.current = chart;

      // Candlestick series
      const candles = chart.addCandlestickSeries({
        upColor:   "#22c55e", downColor: "#ef4444",
        borderUpColor: "#22c55e", borderDownColor: "#ef4444",
        wickUpColor:   "#22c55e", wickDownColor:   "#ef4444",
      });
      // Cast time to UTCTimestamp as required by lightweight-charts v4 strict types
      const typedCandles = data.candles.map(c => ({ ...c, time: c.time as any }));
      candles.setData(typedCandles);
      candleRef.current = candles;

      // S/R horizontal lines
      data.levels.forEach(lvl => {
        const isResist = lvl.type === "resistance";
        const width    = Math.min(lvl.strength, 4);
        const color    = isResist ? "#ef444480" : "#22c55e80";
        candles.createPriceLine({
          price:        lvl.price,
          color:        color,
          lineWidth:    width,
          lineStyle:    LineStyle.Dashed,
          axisLabelVisible: true,
          title:        `${isResist ? "R" : "S"}${lvl.strength}`,
        });
      });

      // Current price line
      if (data.current_price) {
        pricLineRef.current = candles.createPriceLine({
          price:     data.current_price,
          color:     "#f59e0b",
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          axisLabelVisible: true,
          title:     "NOW",
        });
      }

      // Supply zones (red shaded) — draw as area series overlay approximation
      // Lightweight Charts v4 doesn't have native rectangle drawing, so we
      // mark zones via thick dashed lines at zone_top and zone_bot
      data.supply_zones.forEach(z => {
        candles.createPriceLine({ price: z.top,    color: "#ef4444aa", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
        candles.createPriceLine({ price: z.bottom, color: "#ef4444aa", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "Supply" });
      });

      data.demand_zones.forEach(z => {
        candles.createPriceLine({ price: z.top,    color: "#22c55eaa", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "Demand" });
        candles.createPriceLine({ price: z.bottom, color: "#22c55eaa", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
      });

      chart.timeScale().fitContent();

      // Resize observer
      const ro = new ResizeObserver(() => {
        if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(containerRef.current!);
      return () => { ro.disconnect(); };
    });

    return () => { if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; } };
  }, [data]);

  // Update live price line on WebSocket tick
  useEffect(() => {
    if (!livePrice || !pricLineRef.current) return;
    pricLineRef.current.applyOptions({ price: livePrice });
  }, [livePrice]);

  const pos    = data ? POSITION_LABELS[data.position]  : null;
  const struct = data ? STRUCTURE_COLORS[data.structure] : "#94a3b8";

  return (
    <div className="bg-[#0f172a] rounded-xl border border-[#1e293b] overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#1e293b]">
        <div className="flex items-center gap-3">
          <span className="text-white font-semibold text-sm">NIFTY 5m</span>
          {data && (
            <span className="text-xs font-medium px-2 py-0.5 rounded-full"
                  style={{ background: struct + "22", color: struct }}>
              {data.structure.toUpperCase()}
            </span>
          )}
        </div>
        {pos && (
          <span className="text-xs font-medium" style={{ color: pos.color }}>
            ⚡ {pos.label}
          </span>
        )}
        {data && (
          <div className="flex gap-4 text-xs text-slate-400">
            {data.nearest_support    && <span>S: <span className="text-green-400">₹{data.nearest_support.toFixed(0)}</span></span>}
            {data.nearest_resistance && <span>R: <span className="text-red-400">₹{data.nearest_resistance.toFixed(0)}</span></span>}
          </div>
        )}
      </div>

      {/* Chart area */}
      {loading && (
        <div className="h-[420px] flex items-center justify-center text-slate-500 text-sm">
          Loading chart…
        </div>
      )}
      {error && (
        <div className="h-[420px] flex items-center justify-center text-red-400 text-sm">
          Chart unavailable: {error}
        </div>
      )}
      <div ref={containerRef} className={loading || error ? "hidden" : ""} />

      {/* Legend */}
      {data && !loading && (
        <div className="flex gap-4 px-4 py-2 border-t border-[#1e293b] text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dashed border-red-400/60" />
            Resistance
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dashed border-green-400/60" />
            Support
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dotted border-red-400/60" />
            Supply zone
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-dotted border-green-400/60" />
            Demand zone
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-6 border-t-2 border-amber-400" />
            Current price
          </span>
          <span className="ml-auto text-slate-600">{data.levels.length} levels detected</span>
        </div>
      )}
    </div>
  );
}
