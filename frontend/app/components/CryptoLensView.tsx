"use client";
import { useEffect, useRef, useState } from "react";
import { API, authHeaders } from "../lib/format";
import { Card } from "./ui";
import LensTable from "./LensTable";

/* A crypto instrument: its chart, and what the lenses measured on it.
 *
 * The crypto tabs were text-only. That understated the state of the system in
 * both directions — it hid that the council genuinely runs here on the same
 * snapshot type as NIFTY, and it hid the measurements that say why nothing
 * trades. A page that only says "not trading" reads as "not built".
 */

type Candle = { time: number; open: number; high: number; low: number; close: number };

const CHROME = {
  surface: "#0d1117", grid: "#1b222c", border: "#2a3441",
  text: "#c9d3e0", up: "#3fb950", down: "#f85149", dim: "#7d8896",
};

/* Measured on 115,200 five-minute bars per symbol (13 months) — XAUT on 33,109
 * over its own split, because it lists 2026-04-17 and has zero bars in the
 * shared TRAIN window. TRAIN / VALIDATE, in bps.
 */
const RESULTS: Record<string, Array<[string, number, number]>> = {
  BTC: [["vwap", -0.16, 0.95], ["ict_smc", -0.10, 0.18], ["momentum", 0.61, -2.63],
        ["composite_profile", -1.09, 0.46], ["candle_flow", -1.58, -1.34]],
  ETH: [["vwap", 0.26, -1.71], ["ict_smc", 0.38, 1.76], ["momentum", -3.14, -0.05],
        ["composite_profile", 5.15, -8.35], ["candle_flow", -4.52, -3.30]],
  XAUT: [["vwap", -1.70, 0.48], ["ict_smc", -0.26, 0.87], ["momentum", -2.15, -1.10],
         ["composite_profile", -14.19, 3.34], ["candle_flow", -0.45, 0.67]],
};

export default function CryptoLensView({ asset }: { asset: "BTC" | "ETH" | "XAUT" }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<any>(null);
  const seriesRef = useRef<any>(null);
  const didFit = useRef(false);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { didFit.current = false; }, [asset]);

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(
          `${API}/api/crypto/candles?asset=${asset}&resolution=5m&hours=48`,
          { headers: authHeaders() });
        const j = await r.json();
        if (stop) return;
        setCandles(j.candles ?? []);
        setErr(j.error ?? null);
      } catch (e: unknown) {
        if (!stop) setErr(e instanceof Error ? e.message : String(e));
      }
    };
    pull();
    const t = window.setInterval(pull, 15000);
    return () => { stop = true; window.clearInterval(t); };
  }, [asset]);

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
        timeScale: { borderColor: CHROME.border, timeVisible: true, secondsVisible: false },
      });
      chartRef.current = chart;
      seriesRef.current = chart.addCandlestickSeries({
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
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current || candles.length === 0) return;
    seriesRef.current.setData(candles);
    if (!didFit.current && chartRef.current) {
      chartRef.current.timeScale().fitContent();
      didFit.current = true;
    }
  }, [candles]);

  const last = candles.length ? candles[candles.length - 1] : null;
  const results = RESULTS[asset] ?? [];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 10,
                      padding: "14px 16px 12px",
                      borderBottom: "1px solid var(--line)" }}>
          <strong style={{ fontSize: 14 }}>{asset}USD</strong>
          {last && (
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {last.close.toLocaleString()}
            </span>
          )}
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
            perpetual · 24/7 · no session
          </span>
        </div>

        <div style={{ padding: "12px 16px 0" }}>
          <div ref={containerRef} style={{ width: "100%", height: 360 }} />
        </div>

        <div style={{ padding: "10px 16px 14px", fontSize: 11, color: "var(--ink-3)" }}>
          Busiest 18:00–23:00 IST (~2× the movement of 09:00–17:00, both splits).
          That is <b>activity, not edge</b> — it says where risk and sample sit,
          not when to trade.
          {err && <span style={{ color: "var(--down)" }}> · {err}</span>}
        </div>
      </Card>

      <Card>
        <div style={{ padding: "14px 16px 12px", borderBottom: "1px solid var(--line)" }}>
          <strong style={{ fontSize: 14 }}>What was measured on {asset}USD</strong>
          <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
            {" "}· edge in bps, TRAIN / VALIDATE ·{" "}
            {asset === "XAUT" ? "33,109 bars, own split" : "115,200 bars, 13 months"}
          </span>
        </div>
        <div>
          {results.map(([name, tr, va]) => (
            <div key={name} style={{
              display: "grid", gridTemplateColumns: "minmax(120px,1fr) 74px 74px 1fr",
              gap: 8, padding: "9px 16px", fontSize: 12.5, alignItems: "baseline",
              borderBottom: "1px solid var(--line)", color: "var(--ink-2)",
            }}>
              <span>{name}</span>
              <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                             color: tr > 0 ? "var(--up)" : "var(--down)" }}>
                {tr >= 0 ? "+" : ""}{tr.toFixed(2)}
              </span>
              <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                             color: va > 0 ? "var(--up)" : "var(--down)" }}>
                {va >= 0 ? "+" : ""}{va.toFixed(2)}
              </span>
              <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
                {(tr > 0) === (va > 0) ? "signs agree" : "signs disagree"}
              </span>
            </div>
          ))}
        </div>
        <div style={{ padding: "10px 16px", fontSize: 11, color: "var(--ink-3)",
                      lineHeight: 1.6 }}>
          The council <b>does</b> run here — five bar-only lenses on the same
          snapshot type as NIFTY, with the option lenses abstaining on an empty
          chain. None cleared, so every lens carries weight 0 and every entry is
          refused. That is the design working, not a wiring gap.
        </div>
      </Card>

      <LensTable />
    </div>
  );
}
