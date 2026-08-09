"use client";
import { useEffect, useRef, useState } from "react";
import { API, authHeaders } from "../lib/format";
import { Card, Pill } from "./ui";

/* The NIFTY chart, and the council arguing beside it.
 *
 * WHAT THIS IS NOT: the bot's eyes. The vision lens renders its own chart
 * server-side (core/chart/render.py) into a PNG and posts that to the model.
 * That indirection is deliberate — a rendering of OHLC is reproducible, so the
 * vision lens can be replayed bar-for-bar in a backtest, whereas a screenshot
 * of this page could never be. This component is for the person watching.
 *
 * THE LAST CANDLE IS STILL FORMING. It is drawn dimmed and labelled, because a
 * bar that has not closed is not a bar yet — reading it as settled is the same
 * mistake as the fill-at-label lookahead that cost Rs 384,000 in backtest
 * (RESEARCH_LEARNINGS 1.2). The rule deserves to be visible on screen, not just
 * enforced in the harness.
 */

type Candle = { time: number; open: number; high: number; low: number; close: number; volume?: number };

type Verdict = {
  lens: string;
  direction_label: string;
  confidence: number;
  rationale: string;
  abstained: boolean;
  deferred?: boolean;
  revised_from?: number[] | null;
  revision_note?: string;
};

type Decision = {
  decision_id: string;
  ts: string;
  direction_label: string;
  conviction: number;
  executed: boolean;
  reason: string;
  lead?: string | null;
  round1?: Verdict[];
  objections?: { lens: string; direction: string; confidence: number }[];
};

const CHROME = {
  surface: "#0d1117", grid: "#1b222c", border: "#2a3441",
  text: "#c9d3e0", up: "#3fb950", down: "#f85149", dim: "#7d8896",
};

const INTERVALS = ["1m", "5m", "15m"] as const;
type Interval = (typeof INTERVALS)[number];

export default function NiftyChart({ symbol = "NIFTY" }: { symbol?: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<any>(null);
  const seriesRef = useRef<any>(null);
  const didFit = useRef(false);

  const [interval, setInterval_] = useState<Interval>("5m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [err, setErr] = useState<string | null>(null);

  /* ── data ─────────────────────────────────────────────────────────────── */
  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${API}/api/nse/candles?symbol=${symbol}&interval=${interval}`,
                              { headers: authHeaders() });
        const j = await r.json();
        if (stop) return;
        setCandles(j.candles ?? []);
        setErr(j.error ?? null);
      } catch (e: any) {
        if (!stop) setErr(String(e));
      }
    };
    pull();
    const t = window.setInterval(pull, 5000);
    return () => { stop = true; window.clearInterval(t); };
  }, [symbol, interval]);

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${API}/api/nse/council?limit=40`, { headers: authHeaders() });
        const j = await r.json();
        if (!stop) setDecisions(j.decisions ?? []);
      } catch { /* transcript is non-critical; the chart stays up */ }
    };
    pull();
    const t = window.setInterval(pull, 5000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  useEffect(() => { didFit.current = false; }, [interval]);

  /* ── chart ────────────────────────────────────────────────────────────── */
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
        timeScale: {
          borderColor: CHROME.border, timeVisible: true, secondsVisible: false,
          tickMarkFormatter: (t: number) => {
            const d = new Date(t * 1000);
            return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
          },
        },
      });
      chartRef.current = chart;
      // Hollow up / filled down, matching CryptoChart: green-vs-red alone is
      // about 4 dE under deuteranopia, so direction also reads as fill.
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
  const forming = last ? new Date(last.time * 1000) : null;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 340px", gap: 12 }}>
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <strong>{symbol}</strong>
          {last && <span style={{ fontVariantNumeric: "tabular-nums" }}>{last.close.toFixed(2)}</span>}
          <span style={{ flex: 1 }} />
          {INTERVALS.map((iv) => (
            <button key={iv} onClick={() => setInterval_(iv)}
              style={{
                background: iv === interval ? CHROME.border : "transparent",
                color: CHROME.text, border: `1px solid ${CHROME.border}`,
                borderRadius: 4, padding: "2px 8px", cursor: "pointer", fontSize: 12,
              }}>{iv}</button>
          ))}
        </div>

        <div ref={containerRef} style={{ width: "100%", height: 420 }} />

        {forming && (
          <div style={{ marginTop: 6, fontSize: 11, color: CHROME.dim }}>
            last bar {String(forming.getHours()).padStart(2, "0")}:
            {String(forming.getMinutes()).padStart(2, "0")} is still forming — it has not
            closed and is not a settled price
          </div>
        )}
        {err && <div style={{ marginTop: 6, fontSize: 12, color: CHROME.down }}>{err}</div>}
      </Card>

      <Card>
        <div style={{ marginBottom: 8 }}>
          <strong>Council</strong>{" "}
          <span style={{ fontSize: 11, color: CHROME.dim }}>
            every decision, including the ones it declined
          </span>
        </div>
        <div style={{ maxHeight: 470, overflowY: "auto", fontSize: 12 }}>
          {decisions.length === 0 && (
            <div style={{ color: CHROME.dim }}>no decisions journaled yet</div>
          )}
          {decisions.map((d) => (
            <div key={d.decision_id}
                 style={{ borderBottom: `1px solid ${CHROME.grid}`, padding: "6px 0" }}>
              <div style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
                <span style={{ color: CHROME.dim }}>
                  {new Date(d.ts).toLocaleTimeString(undefined, { hour12: false })}
                </span>
                <Pill tone={d.executed ? "up" : "neutral"}>
                  {d.executed ? "EXECUTE" : "stand aside"}
                </Pill>
                <span style={{ color: d.direction_label === "LONG" ? CHROME.up : CHROME.down }}>
                  {d.direction_label}
                </span>
                <span style={{ fontVariantNumeric: "tabular-nums", color: CHROME.dim }}>
                  {d.conviction >= 0 ? "+" : ""}{d.conviction.toFixed(3)}
                </span>
              </div>
              <div style={{ color: CHROME.dim, margin: "2px 0" }}>{d.reason}</div>
              {(d.round1 ?? []).map((v) => (
                <div key={v.lens} style={{ paddingLeft: 8, color: CHROME.dim }}>
                  <span style={{ color: CHROME.text }}>{v.lens}</span>{" "}
                  {v.abstained ? <em>abstains</em>
                    : v.deferred ? <em>defers</em>
                    : <>{v.direction_label} {v.confidence.toFixed(2)}</>}
                  {v.revised_from && <em> (revised)</em>}
                  {" — "}
                  {v.revision_note || v.rationale}
                </div>
              ))}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
