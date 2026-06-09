"use client";
import { useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Candle = { time: number; open: number; high: number; low: number; close: number };

type Props = {
  livePrice?: number;
};

const RESOLUTION = "5m";
const LOOKBACK_HOURS = 24;

export default function CryptoChart({ livePrice }: Props) {
  const [asset, setAsset] = useState<"BTC" | "ETH">("BTC");
  const containerRef = useRef<HTMLDivElement>(null);
  const priceChartRef = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const livePriceLineRef = useRef<any>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Fetch candles on asset change, then refresh every 30s so the x-axis
  // keeps advancing past page-load time (live price extends only the
  // last candle, it doesn't append new ones).
  useEffect(() => {
    let cancelled = false;
    const load = (initial: boolean) => {
      if (initial) { setLoading(true); setErr(null); }
      const token = localStorage.getItem("aq_token");
      const headers = { Authorization: `Bearer ${token}` };
      fetch(`${API_URL}/api/crypto/candles?asset=${asset}&resolution=${RESOLUTION}&hours=${LOOKBACK_HOURS}`, { headers })
        .then(r => r.json())
        .then(candleRes => {
          if (cancelled) return;
          if (candleRes.error && initial) setErr(candleRes.error);
          setCandles(candleRes.candles || []);
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
  }, [asset]);

  // Build/rebuild chart when candles arrive
  useEffect(() => {
    if (!candles.length || !containerRef.current) return;
    import("lightweight-charts").then(({ createChart, LineStyle, CrosshairMode }) => {
      if (priceChartRef.current) { priceChartRef.current.remove(); priceChartRef.current = null; }

      const priceChart = createChart(containerRef.current!, {
        width:  containerRef.current!.clientWidth,
        height: 500,
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

      const last = candles[candles.length - 1];
      livePriceLineRef.current = candleSeries.createPriceLine({
        price: livePrice ?? last.close,
        color: asset === "BTC" ? "#f7931a" : "#627eea",
        lineWidth: 2 as 1 | 2 | 3 | 4,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: "LIVE",
      });

      priceChart.timeScale().fitContent();

      const ro = new ResizeObserver(() => {
        if (containerRef.current) priceChart.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(containerRef.current!);
      return () => ro.disconnect();
    });

    return () => {
      if (priceChartRef.current) { priceChartRef.current.remove(); priceChartRef.current = null; }
    };
  }, [candles, asset]);

  // Live updates: tick the price line + extend the last candle in place
  useEffect(() => {
    if (!livePrice || !candleSeriesRef.current || !livePriceLineRef.current) return;
    livePriceLineRef.current.applyOptions({ price: livePrice });
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
      <div ref={containerRef} className={loading || err ? "hidden" : ""} />
    </div>
  );
}
