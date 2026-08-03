"use client";
import { useEffect, useRef, useState } from "react";
import { API, authHeaders, usd } from "./lib/format";
import { Card, Pill } from "./components/ui";

type Candle = { time: number; open: number; high: number; low: number; close: number };
type Zone = { top: number; bottom: number; price: number; strength: number; volume_norm?: number };

type ChartData = {
  candles: Candle[];
  supply_zones?: Zone[];
  demand_zones?: Zone[];
  structure?: string;
  error?: string;
};

type SignalSummary = {
  stateLabel: string;
  tone: "up" | "down" | "warn" | "brand" | "neutral";
  reason: string;
};

type Props = {
  livePrice?: number | null;
  signal?: SignalSummary | null;
};

const TIMEFRAMES = {
  "5m":  { resolution: "5m",  hours:   24, label: "24h" },
  "15m": { resolution: "15m", hours:   72, label: "3d"  },
  "1h":  { resolution: "1h",  hours:  168, label: "7d"  },
  "4h":  { resolution: "4h",  hours:  720, label: "30d" },
  "1d":  { resolution: "1d",  hours: 2160, label: "90d" },
} as const;
type Timeframe = keyof typeof TIMEFRAMES;

const STRUCTURE_TONE: Record<string, SignalSummary["tone"]> = {
  uptrend: "up", downtrend: "down", ranging: "neutral",
};

// Light-surface chart chrome, matched to the app tokens in globals.css.
const CHROME = {
  surface: "#fcfcfb",
  text:    "#52514e",
  grid:    "#e1e0d9",
  border:  "#c3c2b7",
  up:      "#0ca30c",
  down:    "#d03b3b",
  brand:   "#627eea",
};

export default function CryptoChart({ livePrice, signal }: Props) {
  const [timeframe, setTimeframe] = useState<Timeframe>("5m");
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const livePriceLineRef = useRef<any>(null);
  const zoneLinesRef = useRef<any[]>([]);
  const [data, setData] = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  /* ── Fetch: initial + 30s refresh. Only the DATA changes on refresh; the
        chart instance is created once so zoom/pan survive. ───────────────── */
  useEffect(() => {
    let cancelled = false;
    const tf = TIMEFRAMES[timeframe];
    const load = (initial: boolean) => {
      if (initial) { setLoading(true); setErr(null); }
      fetch(`${API}/api/crypto/candles?asset=ETH&resolution=${tf.resolution}&hours=${tf.hours}`,
            { headers: authHeaders() })
        .then((r) => r.json())
        .then((d: ChartData) => {
          if (cancelled) return;
          if (d.error && initial) setErr(d.error);
          setData(d);
          if (initial) setLoading(false);
        })
        .catch((e) => {
          if (cancelled || !initial) return;
          setErr(e?.message || "Could not load chart data");
          setLoading(false);
        });
    };
    load(true);
    const iv = setInterval(() => load(false), 30_000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [timeframe]);

  /* ── Create the chart ONCE ─────────────────────────────────────────────── */
  useEffect(() => {
    if (!containerRef.current) return;
    let disposed = false;
    let ro: ResizeObserver | null = null;

    import("lightweight-charts").then(({ createChart, CrosshairMode }) => {
      if (disposed || !containerRef.current || chartRef.current) return;

      const chart = createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
        layout: { background: { color: CHROME.surface }, textColor: CHROME.text, fontSize: 11 },
        grid: { vertLines: { color: CHROME.grid }, horzLines: { color: CHROME.grid } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: CHROME.border },
        handleScale: { axisPressedMouseMove: { time: true, price: false } },
        timeScale: {
          borderColor: CHROME.border, timeVisible: true, secondsVisible: false,
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000);
            return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
          },
        },
        localization: {
          timeFormatter: (t: number) =>
            new Date(t * 1000).toLocaleString(undefined, {
              day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
            }),
        },
      });
      chartRef.current = chart;

      // Hollow up-candles / filled down-candles. Green vs red alone measures
      // only ~4 ΔE under deuteranopia, so direction also reads as fill-vs-outline.
      candleSeriesRef.current = chart.addCandlestickSeries({
        upColor: "rgba(0,0,0,0)", downColor: CHROME.down,
        borderUpColor: CHROME.up, borderDownColor: CHROME.down,
        wickUpColor: CHROME.up, wickDownColor: CHROME.down,
        borderVisible: true,
      });

      ro = new ResizeObserver(() => {
        if (disposed || !chartRef.current || !containerRef.current) return;
        try {
          chartRef.current.applyOptions({
            width: containerRef.current.clientWidth,
            height: containerRef.current.clientHeight,
          });
        } catch { /* disposed mid-resize */ }
      });
      ro.observe(containerRef.current);
    });

    return () => {
      disposed = true;
      ro?.disconnect();
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }
      candleSeriesRef.current = null;
      livePriceLineRef.current = null;
      zoneLinesRef.current = [];
    };
  }, []);

  /* ── Push new data into the existing series ────────────────────────────── */
  const didFit = useRef(false);
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series || !data?.candles?.length) return;
    try {
      series.setData(data.candles.map((c) => ({ ...c, time: c.time as any })));

      // Redraw S/R zone edges.
      zoneLinesRef.current.forEach((l) => { try { series.removePriceLine(l); } catch {} });
      zoneLinesRef.current = [];
      const addZone = (z: Zone, color: string, title: string) => {
        [z.top, z.bottom].forEach((price, i) => {
          zoneLinesRef.current.push(series.createPriceLine({
            price, color, lineWidth: 1, lineStyle: 2,
            axisLabelVisible: false, title: i === 0 ? title : "",
          }));
        });
      };
      (data.supply_zones || []).slice(0, 3).forEach((z, i) => addZone(z, CHROME.down + "99", i === 0 ? "Supply" : ""));
      (data.demand_zones || []).slice(0, 3).forEach((z, i) => addZone(z, CHROME.up + "99", i === 0 ? "Demand" : ""));

      if (!didFit.current) { chartRef.current?.timeScale().fitContent(); didFit.current = true; }
    } catch { /* chart disposed */ }
  }, [data]);

  // Refit when the timeframe changes (new data range).
  useEffect(() => { didFit.current = false; }, [timeframe]);

  /* ── Live price line ───────────────────────────────────────────────────── */
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series || !livePrice) return;
    try {
      if (!livePriceLineRef.current) {
        livePriceLineRef.current = series.createPriceLine({
          price: livePrice, color: CHROME.brand, lineWidth: 2,
          lineStyle: 0, axisLabelVisible: true, title: "LIVE",
        });
      } else {
        livePriceLineRef.current.applyOptions({ price: livePrice });
      }
      const last = data?.candles?.[data.candles.length - 1];
      if (last) {
        series.update({
          time: last.time as any, open: last.open,
          high: Math.max(last.high, livePrice), low: Math.min(last.low, livePrice),
          close: livePrice,
        });
      }
    } catch { /* chart disposed */ }
  }, [livePrice, data]);

  const structureTone = data?.structure ? STRUCTURE_TONE[data.structure] ?? "neutral" : null;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3 sm:px-4 py-3 border-b border-[var(--line)] flex-wrap">
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          <h2 className="card-title">ETHUSD</h2>
          {livePrice != null && (
            <span className="text-sm font-semibold tnum text-[var(--ink)]">{usd(livePrice)}</span>
          )}
          {structureTone && data?.structure && (
            <Pill tone={structureTone}>{data.structure}</Pill>
          )}
          {signal && <Pill tone={signal.tone} title={signal.reason}>{signal.stateLabel}</Pill>}
        </div>

        <div className="flex items-center gap-0.5 p-0.5 rounded-[var(--r-sm)] bg-[var(--surface-2)] border border-[var(--line)]"
             role="group" aria-label="Timeframe">
          {(Object.keys(TIMEFRAMES) as Timeframe[]).map((tf) => (
            <button key={tf} onClick={() => setTimeframe(tf)}
                    aria-pressed={timeframe === tf}
                    className={`px-2 sm:px-2.5 py-1 text-[11px] font-semibold rounded-[6px] transition-colors ${
                      timeframe === tf
                        ? "bg-[var(--surface)] text-[var(--ink)] shadow-[var(--shadow-sm)]"
                        : "text-[var(--ink-3)] hover:text-[var(--ink-2)]"
                    }`}>
              {tf}
            </button>
          ))}
        </div>
      </div>

      <div className="relative">
        {/* Responsive height: shorter on phones, taller on desktop. */}
        <div ref={containerRef} className="w-full h-[260px] sm:h-[380px] lg:h-[440px]" />
        {(loading || err) && (
          <div className="absolute inset-0 grid place-items-center bg-[var(--surface)]">
            {loading
              ? <div className="text-center"><div className="skel h-4 w-32 mx-auto mb-2" /><p className="text-xs text-[var(--ink-3)]">Loading chart…</p></div>
              : <p className="text-sm text-[var(--down-ink)] px-4 text-center">{err}</p>}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 sm:px-4 py-2 border-t border-[var(--line)] text-[11px] text-[var(--ink-3)]">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 border-[1.5px] rounded-[2px]" style={{ borderColor: CHROME.up }} aria-hidden="true" />
          Up candle (hollow)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 rounded-[2px]" style={{ background: CHROME.down }} aria-hidden="true" />
          Down candle (filled)
        </span>
        <span className="hidden sm:inline">Dashed lines mark 4h supply / demand zones</span>
      </div>
    </Card>
  );
}
