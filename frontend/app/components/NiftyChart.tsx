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

type Level = { price: number; label: string; kind: string };
type Levels = {
  as_of?: string; spot?: number; direction?: string; executed?: boolean;
  levels: Level[];
  structure?: { trend?: string | null; order_block?: number | null;
                fvg?: number | null; sweep?: number | null };
};
type Marker = {
  time: number; executed: boolean; direction: number; conviction: number;
  lead?: string | null; text: string; reason: string;
};
type Expectation = {
  from_time: number; to_time: number; entry: number; direction: number;
  horizon_min: number; target: number; band_high: number; band_low: number;
  basis: string;
};

/* What the council is reading, drawn where it is reading it.
 *
 * Every one of these levels is already computed on every decision and journaled
 * — OI walls, composite POC/VAH/VAL, naked POCs, the gamma flip, range extremes.
 * None of it reached the screen, so the chart showed price while the council
 * reasoned about structure the operator could not see.
 *
 * Colours carry MEANING, not decoration: supply above, demand below, magnets
 * (untested POCs) dashed because price has unfinished business there rather
 * than support that has held.
 */
const LEVEL_STYLE: Record<string, { color: string; dashed: boolean }> = {
  supply: { color: "#f85149", dashed: false },
  demand: { color: "#3fb950", dashed: false },
  value:  { color: "#58a6ff", dashed: false },
  magnet: { color: "#d29922", dashed: true },
  pivot:  { color: "#bc8cff", dashed: true },
};

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
  const [levels, setLevels] = useState<Levels | null>(null);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [expect, setExpect] = useState<Expectation | null>(null);
  const [box, setBox] = useState<{left:number;width:number;yEntry:number;yTarget:number;yStop:number} | null>(null);
  const priceLinesRef = useRef<any[]>([]);
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

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const [l, m] = await Promise.all([
          fetch(`${API}/api/nse/levels?symbol=${symbol}`, { headers: authHeaders() }),
          fetch(`${API}/api/nse/markers?symbol=${symbol}&limit=200`, { headers: authHeaders() }),
        ]);
        const lj = await l.json();
        const mj = await m.json();
        if (stop) return;
        setLevels(lj);
        setMarkers(mj.markers ?? []);
        setExpect(mj.expectation ?? null);
      } catch { /* overlays are non-critical; the chart stays up without them */ }
    };
    pull();
    const t = window.setInterval(pull, 10000);
    return () => { stop = true; window.clearInterval(t); };
  }, [symbol]);

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

  // Horizontal levels. Removed and redrawn wholesale rather than diffed: there
  // are a dozen of them and a stale line showing a level the council has since
  // moved off is worse than a redraw flicker.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    for (const pl of priceLinesRef.current) {
      try { series.removePriceLine(pl); } catch { /* series may be disposed */ }
    }
    priceLinesRef.current = [];
    for (const lv of levels?.levels ?? []) {
      const st = LEVEL_STYLE[lv.kind] ?? { color: "#7d8896", dashed: true };
      try {
        priceLinesRef.current.push(series.createPriceLine({
          price: lv.price,
          color: st.color,
          lineWidth: 1,
          lineStyle: st.dashed ? 2 : 0,
          axisLabelVisible: true,
          title: lv.label,
        }));
      } catch { /* ignore a level the chart rejects */ }
    }
  }, [levels]);

  // THE TRADE BOX, drawn as an overlay rather than as price lines.
  //
  // lightweight-charts has no rectangle primitive, so the zones are positioned
  // by converting price and time to pixel coordinates on every frame the chart
  // moves. That is what makes a target/stop box possible at all here without
  // pulling in a second charting library.
  //
  // Green above entry is the measured target zone, red below is the risk. They
  // are drawn to SCALE, which is the point: the measured edge is +1.67 bps, so
  // on a 24,400 index the green band is about four points tall. A box sized to
  // look impressive would be a lie about magnitude, and the honest one being
  // thin is the most useful thing on the chart.
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    const host = containerRef.current;
    if (!chart || !series || !host || !expect) { setBox(null); return; }

    const draw = () => {
      try {
        const ts = chart.timeScale();
        const x1 = ts.timeToCoordinate(expect.from_time as any);
        const x2 = ts.timeToCoordinate(expect.to_time as any);
        const yEntry = series.priceToCoordinate(expect.entry);
        const yTarget = series.priceToCoordinate(expect.target);
        const yStop = series.priceToCoordinate(
          expect.direction > 0 ? expect.band_low : expect.band_high);
        if (x1 == null || yEntry == null || yTarget == null || yStop == null) {
          setBox(null);
          return;
        }
        // The horizon may extend past the last bar, so the right edge is
        // clamped to the plot rather than allowed to run off it.
        const right = x2 == null ? host.clientWidth - 80 : Math.min(x2, host.clientWidth - 80);
        setBox({
          left: x1, width: Math.max(24, right - x1),
          yEntry, yTarget, yStop,
        });
      } catch { setBox(null); }
    };

    draw();
    const ts = chart.timeScale();
    ts.subscribeVisibleTimeRangeChange(draw);
    return () => {
      try { ts.unsubscribeVisibleTimeRangeChange(draw); } catch { /* disposed */ }
    };
  }, [expect, candles]);

  // Decision markers, EXECUTED AND DECLINED. The declined ones are the point:
  // seeing where the council stood aside against what price then did is how you
  // judge whether the conviction gate is protecting you or costing you. A chart
  // showing only fills would make the system look far more active, and far more
  // right, than it is.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || markers.length === 0) return;
    // EXECUTED ONLY.
    //
    // Declined decisions were drawn too, on the argument that seeing where the
    // council stood aside is informative. At 200 of them that argument fails on
    // contact: the arrows covered every candle and buried the price action they
    // were meant to be judged against. Information that hides the thing it
    // annotates is not information.
    //
    // The count still appears beneath the chart, and every declined decision is
    // in the transcript with its reason — so nothing is lost except the clutter.
    const shown = markers.filter((m) => m.executed);
    try {
      series.setMarkers(shown.map((m) => ({
        time: m.time,
        position: m.direction > 0 ? "belowBar" : "aboveBar",
        shape: m.direction > 0 ? "arrowUp" : "arrowDown",
        color: m.direction > 0 ? CHROME.up : CHROME.down,
        text: m.text,
      })));
    } catch { /* marker times outside the loaded range */ }
  }, [markers]);

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

        <div style={{ position: "relative", width: "100%", height: 420 }}>
          <div ref={containerRef} style={{ width: "100%", height: "100%" }} />

          {box && expect && (
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
              {/* reward zone */}
              <div style={{
                position: "absolute", left: box.left, width: box.width,
                top: Math.min(box.yEntry, box.yTarget),
                height: Math.max(2, Math.abs(box.yEntry - box.yTarget)),
                background: "rgba(63,185,80,0.18)",
                border: "1px solid rgba(63,185,80,0.55)",
              }} />
              {/* risk zone */}
              <div style={{
                position: "absolute", left: box.left, width: box.width,
                top: Math.min(box.yEntry, box.yStop),
                height: Math.max(2, Math.abs(box.yEntry - box.yStop)),
                background: "rgba(248,81,73,0.14)",
                border: "1px solid rgba(248,81,73,0.45)",
              }} />
              {/* entry */}
              <div style={{
                position: "absolute", left: box.left, width: box.width,
                top: box.yEntry, borderTop: "1px dashed rgba(201,211,224,0.8)",
              }} />
              <span style={{
                position: "absolute", left: box.left + 4,
                top: Math.min(box.yEntry, box.yTarget) - 16,
                fontSize: 10, color: "#3fb950", whiteSpace: "nowrap",
                background: "rgba(13,17,23,0.75)", padding: "1px 4px", borderRadius: 3,
              }}>
                target {expect.target.toFixed(1)} · {expect.horizon_min}m
              </span>
              <span style={{
                position: "absolute", left: box.left + 4,
                top: Math.max(box.yEntry, box.yStop) + 2,
                fontSize: 10, color: "#f85149", whiteSpace: "nowrap",
                background: "rgba(13,17,23,0.75)", padding: "1px 4px", borderRadius: 3,
              }}>
                1sd risk {(expect.direction > 0 ? expect.band_low : expect.band_high).toFixed(1)}
              </span>
            </div>
          )}
        </div>

        {(levels?.levels?.length ?? 0) > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 8,
                        fontSize: 11, color: CHROME.dim }}>
            {levels!.levels.map((lv) => {
              const st = LEVEL_STYLE[lv.kind] ?? { color: "#7d8896", dashed: true };
              return (
                <span key={`${lv.label}-${lv.price}`}
                      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 14, height: 0,
                                 borderTop: `2px ${st.dashed ? "dashed" : "solid"} ${st.color}` }} />
                  {lv.label} {lv.price.toFixed(0)}
                </span>
              );
            })}
            {levels?.structure?.trend && (
              <span>· structure: {levels.structure.trend}</span>
            )}
          </div>
        )}

        {expect && (
          <div style={{
            marginTop: 8, padding: "8px 10px", borderRadius: 6,
            border: `1px solid ${expect.direction > 0 ? CHROME.up : CHROME.down}33`,
            background: expect.direction > 0
              ? "rgba(63,185,80,0.06)" : "rgba(248,81,73,0.06)",
            fontSize: 12,
          }}>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap",
                          alignItems: "baseline" }}>
              <strong style={{ color: expect.direction > 0 ? CHROME.up : CHROME.down }}>
                Expectation
              </strong>
              <span style={{ fontVariantNumeric: "tabular-nums" }}>
                {expect.entry.toFixed(1)} → {expect.target.toFixed(1)}
                {"  "}({(expect.target - expect.entry >= 0 ? "+" : "")}
                {(expect.target - expect.entry).toFixed(1)} pts)
              </span>
              <span style={{ color: CHROME.dim }}>
                over {expect.horizon_min}m · band {expect.band_low.toFixed(0)}–
                {expect.band_high.toFixed(0)}
              </span>
            </div>
            <div style={{ color: CHROME.dim, marginTop: 3 }}>
              {expect.basis}. This is the MEASURED expectation, not a forecast
              path — the system produces a direction and a horizon, not a
              trajectory.
            </div>
          </div>
        )}

        {markers.length > 0 && (
          <div style={{ marginTop: 4, fontSize: 11, color: CHROME.dim }}>
            {markers.filter((m) => m.executed).length} executed (arrows) ·{" "}
            {markers.filter((m) => !m.executed).length} stood aside — reasons in
            the transcript, not drawn
          </div>
        )}

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
        <div style={{ maxHeight: 470, overflowY: "auto" }}>
          {decisions.length === 0 && (
            <div style={{ color: CHROME.dim, fontSize: 12 }}>
              no decisions journaled yet
            </div>
          )}
          {decisions.map((d) => {
            // Only the lenses that SAID something lead the entry. Abstentions
            // are collapsed to a count: on a typical snapshot five of nine
            // abstain for structural reasons (DTE, empty chain, too few bars),
            // and printing five identical apologies per decision is what turned
            // this panel into a wall of text.
            const spoke = (d.round1 ?? []).filter(
              (v) => !v.abstained && !v.deferred);
            const quiet = (d.round1 ?? []).length - spoke.length;
            return (
              <details key={d.decision_id} open={d.executed}
                       style={{ borderBottom: `1px solid ${CHROME.grid}`,
                                padding: "8px 0" }}>
                <summary style={{ cursor: "pointer", listStyle: "none",
                                  display: "flex", gap: 8, alignItems: "baseline",
                                  fontSize: 12, flexWrap: "wrap" }}>
                  <span style={{ color: CHROME.dim, fontVariantNumeric: "tabular-nums" }}>
                    {new Date(d.ts).toLocaleTimeString(undefined,
                      { hour12: false, hour: "2-digit", minute: "2-digit" })}
                  </span>
                  <Pill tone={d.executed ? "up" : "neutral"}>
                    {d.executed ? "EXECUTE" : "aside"}
                  </Pill>
                  <span style={{ color: d.direction_label === "LONG" ? CHROME.up : CHROME.down,
                                 fontWeight: 600 }}>
                    {d.direction_label}
                  </span>
                  <span style={{ color: CHROME.dim, fontVariantNumeric: "tabular-nums" }}>
                    {d.conviction >= 0 ? "+" : ""}{d.conviction.toFixed(2)}
                  </span>
                  {d.lead && (
                    <span style={{ color: CHROME.dim, fontSize: 11 }}>
                      lead {d.lead}
                    </span>
                  )}
                  <span style={{ flex: 1 }} />
                  <span style={{ color: CHROME.dim, fontSize: 10 }}>
                    {spoke.length} spoke · {quiet} quiet
                  </span>
                </summary>

                <div style={{ fontSize: 11, color: CHROME.dim, margin: "4px 0 6px" }}>
                  {d.reason}
                </div>

                {spoke.map((v) => (
                  <div key={v.lens} style={{
                    display: "grid", gridTemplateColumns: "104px 62px 1fr",
                    gap: 6, padding: "2px 0", fontSize: 11, alignItems: "baseline",
                  }}>
                    <span style={{ color: CHROME.text }}>
                      {v.lens}{v.revised_from ? " ↻" : ""}
                    </span>
                    <span style={{
                      color: v.direction_label === "LONG" ? CHROME.up
                           : v.direction_label === "SHORT" ? CHROME.down : CHROME.dim,
                      fontVariantNumeric: "tabular-nums",
                    }}>
                      {v.direction_label === "NEUTRAL" ? "—"
                        : `${v.direction_label === "LONG" ? "L" : "S"} ${v.confidence.toFixed(2)}`}
                    </span>
                    <span style={{ color: CHROME.dim }}>
                      {v.revision_note || v.rationale}
                    </span>
                  </div>
                ))}
                {quiet > 0 && (
                  <div style={{ fontSize: 10, color: CHROME.dim, marginTop: 4,
                                fontStyle: "italic" }}>
                    {quiet} abstained or deferred — expand a lens list in the
                    journal for the reasons
                  </div>
                )}
              </details>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
