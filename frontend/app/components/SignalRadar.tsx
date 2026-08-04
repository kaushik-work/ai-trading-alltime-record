"use client";
import { Card, CardHead, Empty, Pill } from "./ui";
import { pct, usd } from "../lib/format";

export type SignalRow = {
  underlying: string;
  spot: number;
  width_pct: number;
  r_high: number;
  r_low: number;
  trend: "bullish" | "bearish" | "neutral";
  trend_ma?: number;
  near_support: boolean;
  near_resistance: boolean;
  wick_touch_support: boolean;
  wick_touch_resistance: boolean;
  strong_green: boolean;
  strong_red: boolean;
  in_cooldown: boolean;
  time_ok?: boolean;
  block_long?: boolean;
  block_short?: boolean;
  sl_pct: number;
  tp_pct: number;
  vol_24h?: number;
  vol_filter_ok?: boolean;
  candles_count?: number;
  warmup_target?: number;
  warmup_pct?: number;
  side: "buy" | "sell" | null;
  ready: boolean;
};

type Gate = { label: string; pass: boolean; detail?: string };

/** The strategy's entry conditions, in the order it evaluates them. Showing
 *  the checklist beats a tooltip: you can see exactly what it is waiting for. */
function gates(s: SignalRow): Gate[] {
  const bullish = s.trend === "bullish";
  const bearish = s.trend === "bearish";
  const atLevel = bullish ? s.near_support : bearish ? s.near_resistance : (s.near_support || s.near_resistance);
  const wick = bullish ? s.wick_touch_support : bearish ? s.wick_touch_resistance : (s.wick_touch_support || s.wick_touch_resistance);
  const candle = bullish ? s.strong_green : bearish ? s.strong_red : (s.strong_green || s.strong_red);
  const blocked = bullish ? !!s.block_long : bearish ? !!s.block_short : (!!s.block_long && !!s.block_short);

  const list: Gate[] = [
    { label: "Warmed up", pass: s.ready, detail: s.ready ? "24h of candles" : `${s.candles_count ?? 0}/${s.warmup_target ?? 1440}` },
    { label: "Trend defined", pass: bullish || bearish, detail: s.trend },
    { label: "At 4h S/R edge", pass: atLevel, detail: `range ${s.width_pct.toFixed(2)}%` },
    { label: "Wick touched level", pass: wick },
    { label: "Strong reversal candle", pass: candle },
  ];
  if (s.vol_filter_ok !== undefined) {
    list.push({
      label: "Volatility in range",
      pass: s.vol_filter_ok !== false,
      detail: s.vol_24h != null ? `${(s.vol_24h * 100).toFixed(1)}% / 34% max` : undefined,
    });
  }
  list.push({ label: "Cooldown clear", pass: !s.in_cooldown });
  if (s.block_long || s.block_short) list.push({ label: "Not blocked after loss", pass: !blocked });
  if (s.time_ok === false) list.push({ label: "Within trading hours", pass: false });
  return list;
}

function stateOf(s: SignalRow): { label: string; tone: "up" | "down" | "warn" | "brand" | "neutral" } {
  if (s.side === "buy") return { label: "LONG firing", tone: "up" };
  if (s.side === "sell") return { label: "SHORT firing", tone: "down" };
  if (!s.ready) return { label: "Warming up", tone: "warn" };
  if (s.in_cooldown) return { label: "Cooldown", tone: "neutral" };
  const g = gates(s);
  const blocker = g.find((x) => !x.pass);
  // Don't lowercase — it mangles "S/R" into "s/r".
  return { label: blocker ? `Waiting: ${blocker.label}` : "Armed", tone: blocker ? "neutral" : "brand" };
}

export default function SignalRadar({ signals, loading }: { signals: SignalRow[]; loading?: boolean }) {
  const firing = signals.filter((s) => s.side != null).length;

  return (
    <Card>
      <CardHead
        title="Signal radar"
        sub="No active strategy — retired 2026-08-04"
        right={firing > 0
          ? <Pill tone="up"><span aria-hidden="true">▲</span>{firing} firing</Pill>
          : <Pill tone="neutral">Idle</Pill>}
      />

      {loading ? (
        <div className="card-pad space-y-3">
          <div className="skel h-6 w-32" /><div className="skel h-24 w-full" />
        </div>
      ) : signals.length === 0 ? (
        <Empty icon="◎" title="No signal data yet" hint="Waiting for the first snapshot from the bot." />
      ) : (
        <div className="divide-y divide-[var(--line)]">
          {signals.map((s, i) => {
            const st = stateOf(s);
            const g = gates(s);
            const passed = g.filter((x) => x.pass).length;
            return (
              <div key={i} className="card-pad">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-base font-semibold text-[var(--ink)]">{s.underlying}USD</span>
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </div>
                    <p className="text-xs text-[var(--ink-3)] mt-1 tnum">
                      {usd(s.spot)} · 4h range {usd(s.r_low)}–{usd(s.r_high)}
                    </p>
                  </div>
                  <div className="text-right text-xs tnum">
                    <p className="text-[var(--down-ink)] font-semibold">Stop {pct(s.sl_pct * 100)}</p>
                    <p className="text-[var(--up-ink)] font-semibold mt-0.5">Target {pct(s.tp_pct * 100)}</p>
                  </div>
                </div>

                {/* Warm-up progress */}
                {!s.ready && (
                  <div className="mt-3">
                    <div className="flex justify-between text-[11px] text-[var(--ink-3)] mb-1 tnum">
                      <span>Building candle history</span>
                      <span>{s.warmup_pct ?? 0}%</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-[var(--surface-2)] overflow-hidden">
                      <div className="h-full rounded-full transition-[width] duration-500"
                           style={{ width: `${s.warmup_pct ?? 0}%`, background: "var(--brand)" }} />
                    </div>
                  </div>
                )}

                {/* Entry checklist */}
                <div className="mt-4">
                  <p className="label mb-2">Entry conditions · {passed}/{g.length} met</p>
                  <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5">
                    {g.map((x) => (
                      <li key={x.label} className="flex items-center gap-2 text-[13px] min-w-0">
                        <span aria-hidden="true"
                              className={`flex-none w-4 h-4 rounded-full grid place-items-center text-[10px] font-bold ${
                                x.pass ? "bg-[var(--up-wash)] text-[var(--up-ink)]"
                                       : "bg-[var(--surface-2)] text-[var(--ink-3)]"}`}>
                          {x.pass ? "✓" : "·"}
                        </span>
                        <span className={`truncate ${x.pass ? "text-[var(--ink-2)]" : "text-[var(--ink-3)]"}`}>
                          {x.label}
                        </span>
                        {x.detail && (
                          <span className="ml-auto text-[11px] text-[var(--ink-3)] tnum flex-none">{x.detail}</span>
                        )}
                        <span className="sr-only">{x.pass ? "met" : "not met"}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
