"use client";
import { useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Candle = { time: number; open: number; high: number; low: number; close: number };
type SignalSample = { ts: number; pred_pct: number; n_strikes: number };

type Props = {
  livePrice?: number;
  liveSignals?: { underlying: string; pred_pct: number }[];
  gatePct?: number;
};

const RESOLUTION = "5m";
const LOOKBACK_HOURS = 24;

export default function CryptoChart({ livePrice, liveSignals, gatePct = 0.6 }: Props) {
  const [asset, setAsset] = useState<"BTC" | "ETH">("BTC");
  const containerRef = useRef<HTMLDivElement>(null);
  const sigContainerRef = useRef<HTMLDivElement>(null);
  const priceChartRef = useRef<any>(null);
  const sigChartRef = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const sigSeriesRef = useRef<any>(null);
  const livePriceLineRef = useRef<any>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [sigSamples, setSigSamples] = useState<SignalSample[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Fetch candles + signal history when asset changes
  useEffect(() => {
    setLoading(true);
    setErr(null);
    const token = localStorage.getItem("aq_token");
    const headers = { Authorization: `Bearer ${token}` };
    Promise.all([
      fetch(`${API_URL}/api/crypto/candles?asset=${asset}&resolution=${RESOLUTION}&hours=${LOOKBACK_HOURS}`, { headers })
        .then(r => r.json()),
      fetch(`${API_URL}/api/crypto/signal-history?asset=${asset}&hours=${LOOKBACK_HOURS}`, { headers })
        .then(r => r.json()),
    ])
      .then(([candleRes, sigRes]) => {
        if (candleRes.error) setErr(candleRes.error);
        setCandles(candleRes.candles || []);
        setSigSamples(sigRes.samples || []);
        setLoading(false);
      })
      .catch(e => { setErr(e?.message || "Network error"); setLoading(false); });
  }, [asset]);

  // Build/rebuild chart when candles arrive
  useEffect(() => {
    if (!candles.length || !containerRef.current || !sigContainerRef.current) return;
    import("lightweight-charts").then(({ createChart, LineStyle, CrosshairMode }) => {
      if (priceChartRef.current) { priceChartRef.current.remove(); priceChartRef.current = null; }
      if (sigChartRef.current)   { sigChartRef.current.remove();   sigChartRef.current = null; }

      // ── Price chart (top) ──────────────────────────────────────────────
      const priceChart = createChart(containerRef.current!, {
        width:  containerRef.current!.clientWidth,
        height: 360,
        layout: { background: { color: "#0e0e1a" }, textColor: "#94a3b8" },
        grid:   { vertLines: { color: "#1e1e30" }, horzLines: { color: "#1e1e30" } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#1e1e30" },
        timeScale: {
          borderColor: "#1e1e30",
          timeVisible: true,
          secondsVisible: false,
        },
      });
      priceChartRef.current = priceChart;

      const candleSeries = priceChart.addCandlestickSeries({
        upColor:   "#22c55e", downColor: "#ef4444",
        borderUpColor: "#22c55e", borderDownColor: "#ef4444",
        wickUpColor:   "#22c55e", wickDownColor:   "#ef4444",
      });
      candleSeries.setData(candles.map(c => ({ ...c, time: c.time as any })));
      candleSeriesRef.current = candleSeries;

      // Current price line
      const last = candles[candles.length - 1];
      livePriceLineRef.current = candleSeries.createPriceLine({
        price: livePrice ?? last.close,
        color: asset === "BTC" ? "#f7931a" : "#627eea",
        lineWidth: 2 as 1 | 2 | 3 | 4,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: "LIVE",
      });

      // ── Signal chart (bottom, smaller) ─────────────────────────────────
      const sigChart = createChart(sigContainerRef.current!, {
        width:  sigContainerRef.current!.clientWidth,
        height: 140,
        layout: { background: { color: "#0e0e1a" }, textColor: "#94a3b8" },
        grid:   { vertLines: { color: "#1e1e30" }, horzLines: { color: "#1e1e30" } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#1e1e30" },
        timeScale: { borderColor: "#1e1e30", timeVisible: true, secondsVisible: false },
      });
      sigChartRef.current = sigChart;

      const sigSeries = sigChart.addLineSeries({
        color: "#f7931a",
        lineWidth: 2 as 1 | 2 | 3 | 4,
        priceLineVisible: false,
        title: "pred%",
      });
      sigSeries.setData(sigSamples.map(s => ({ time: s.ts as any, value: s.pred_pct })));
      sigSeriesRef.current = sigSeries;

      // Gate lines on the signal chart (+/- 0.6%)
      sigSeries.createPriceLine({
        price:  gatePct, color: "#22c55e80",
        lineWidth: 1 as 1 | 2 | 3 | 4, lineStyle: LineStyle.Dashed,
        axisLabelVisible: true, title: `gate +${gatePct}%`,
      });
      sigSeries.createPriceLine({
        price: -gatePct, color: "#ef444480",
        lineWidth: 1 as 1 | 2 | 3 | 4, lineStyle: LineStyle.Dashed,
        axisLabelVisible: true, title: `gate -${gatePct}%`,
      });
      sigSeries.createPriceLine({
        price: 0, color: "#555",
        lineWidth: 1 as 1 | 2 | 3 | 4, lineStyle: LineStyle.Dotted,
        axisLabelVisible: false, title: "",
      });

      // The price chart owns the time axis (it has the full 24h of candles).
      // The signal chart follows — its trace data is only a few minutes wide
      // at startup, so without this anchor it would zoom itself down to that
      // tiny window and (via reverse-sync) drag the price chart with it.
      priceChart.timeScale().fitContent();
      const earliestT = candles[0]?.time;
      const latestT   = candles[candles.length - 1]?.time;
      if (earliestT && latestT) {
        sigChart.timeScale().setVisibleRange({
          from: earliestT as any, to: latestT as any,
        });
      }
      priceChart.timeScale().subscribeVisibleLogicalRangeChange(r => {
        if (r) sigChart.timeScale().setVisibleLogicalRange(r);
      });

      const ro = new ResizeObserver(() => {
        if (containerRef.current) priceChart.applyOptions({ width: containerRef.current.clientWidth });
        if (sigContainerRef.current) sigChart.applyOptions({ width: sigContainerRef.current.clientWidth });
      });
      ro.observe(containerRef.current!);
      ro.observe(sigContainerRef.current!);
      return () => ro.disconnect();
    });

    return () => {
      if (priceChartRef.current) { priceChartRef.current.remove(); priceChartRef.current = null; }
      if (sigChartRef.current)   { sigChartRef.current.remove();   sigChartRef.current = null; }
    };
  }, [candles, sigSamples, asset]);

  // Live updates: tick the price line + extend the last candle
  useEffect(() => {
    if (!livePrice || !candleSeriesRef.current || !livePriceLineRef.current) return;
    livePriceLineRef.current.applyOptions({ price: livePrice });
    // Extend the latest candle with live price (open/high/low maintained)
    if (candles.length) {
      const last = candles[candles.length - 1];
      const hi = Math.max(last.high, livePrice);
      const lo = Math.min(last.low,  livePrice);
      candleSeriesRef.current.update({
        time: last.time as any,
        open: last.open, high: hi, low: lo, close: livePrice,
      });
    }
  }, [livePrice]);

  // Live updates: append latest signal sample, bucketed to 5-min boundaries.
  // Without bucketing, every 2s WS push would advance the time axis and the
  // chart would scroll left continuously. Bucketing means we only ADVANCE
  // the x-axis once per 5min — between buckets, update() rewrites the same
  // point in place.
  useEffect(() => {
    if (!liveSignals || !sigSeriesRef.current) return;
    const matchingSig = liveSignals.find(s => s.underlying === asset);
    if (!matchingSig) return;
    const nowSec = Math.floor(Date.now() / 1000);
    const bucketSec = nowSec - (nowSec % 300);
    sigSeriesRef.current.update({ time: bucketSec as any, value: matchingSig.pred_pct });
  }, [liveSignals, asset]);

  return (
    <div className="border border-[#1e1e30] rounded-2xl overflow-hidden bg-[#0e0e1a]">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1e1e30]">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold">
            <span className={asset === "BTC" ? "text-[#f7931a]" : "text-[#627eea]"}>
              {asset}USD
            </span>
            <span className="text-gray-500 font-normal ml-2 text-xs">5m · last 24h</span>
          </h3>
          {livePrice && (
            <span className="text-xs text-gray-400 font-mono">
              ${livePrice.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            </span>
          )}
        </div>
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

      {loading && (
        <div className="h-[500px] flex items-center justify-center text-gray-500 text-sm">
          Loading {asset} chart…
        </div>
      )}
      {err && !loading && (
        <div className="h-[500px] flex items-center justify-center text-red-400 text-sm">
          {err}
        </div>
      )}
      <div className={loading || err ? "hidden" : ""}>
        <div ref={containerRef} />
        <div className="px-4 py-1 text-[10px] text-gray-500 border-t border-[#1e1e30]">
          synth-forward pred% (C − P + K vs spot)
        </div>
        <div ref={sigContainerRef} />
      </div>
    </div>
  );
}
