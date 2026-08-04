"use client";
import { Card, CardHead, Empty, Pill, Signed } from "./ui";
import { duration, usd, signedUsd, tone, arrow } from "../lib/format";

export type Position = {
  strategy: string;
  symbol: string;
  side: "buy" | "sell";
  entry_price: number;
  mark_price: number | null;
  contracts: number;
  notional_usd: number;
  unrealized_pct: number;
  unrealized_usd: number;
  stop_price: number;
  target_price: number;
  stop_pct: number;
  target_pct: number;
  to_stop_pct: number | null;
  to_target_pct: number | null;
  held_minutes: number;
  max_hold_minutes: number;
  peak_pct: number;
};

/** Where price sits between stop and target, 0–100. */
function bracketProgress(p: Position): number {
  const lo = Math.min(p.stop_price, p.target_price);
  const hi = Math.max(p.stop_price, p.target_price);
  const mark = p.mark_price ?? p.entry_price;
  if (hi <= lo) return 50;
  return Math.max(0, Math.min(100, ((mark - lo) / (hi - lo)) * 100));
}

function entryMarker(p: Position): number {
  const lo = Math.min(p.stop_price, p.target_price);
  const hi = Math.max(p.stop_price, p.target_price);
  if (hi <= lo) return 50;
  return Math.max(0, Math.min(100, ((p.entry_price - lo) / (hi - lo)) * 100));
}

export default function PositionCard({ positions, loading }: {
  positions: Position[]; loading?: boolean;
}) {
  return (
    <Card>
      <CardHead
        title="Open position"
        right={positions.length > 0
          ? <Pill tone="brand">{positions.length} live</Pill>
          : <Pill tone="neutral">Flat</Pill>}
      />
      {loading ? (
        <div className="card-pad space-y-3">
          <div className="skel h-8 w-40" /><div className="skel h-2 w-full" /><div className="skel h-16 w-full" />
        </div>
      ) : positions.length === 0 ? (
        <Empty icon="○" title="No position open"
               hint="The bot is flat and will stay flat — no strategy is active. Signal generation was retired after every candidate measured negative out-of-sample." />
      ) : (
        <div className="divide-y divide-[var(--line)]">
          {positions.map((p) => {
            const isLong = p.side === "buy";
            const t = tone(p.unrealized_pct);
            const progress = bracketProgress(p);
            const entryAt = entryMarker(p);
            // Stop sits at the low end for a long, the high end for a short.
            const stopOnLeft = p.stop_price < p.target_price;

            return (
              <div key={p.strategy} className="card-pad">
                {/* Headline row */}
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-base font-semibold text-[var(--ink)]">{p.symbol}</span>
                      <Pill tone={isLong ? "up" : "down"}>
                        <span aria-hidden="true">{isLong ? "▲" : "▼"}</span>
                        {isLong ? "LONG" : "SHORT"}
                      </Pill>
                    </div>
                    <p className="text-xs text-[var(--ink-3)] mt-1 tnum">
                      {p.contracts} contracts · {usd(p.notional_usd, 0)} notional
                    </p>
                  </div>
                  <div className="text-right">
                    <p className={`text-2xl font-semibold tnum ${
                      t === "up" ? "text-[var(--up-ink)]" : t === "down" ? "text-[var(--down-ink)]" : "text-[var(--ink)]"
                    }`}>
                      <span aria-hidden="true" className="text-base">{arrow(p.unrealized_pct)}</span>{" "}
                      {signedUsd(p.unrealized_usd)}
                    </p>
                    <p className="text-xs mt-0.5"><Signed value={p.unrealized_pct} /></p>
                  </div>
                </div>

                {/* Bracket track: stop ─────●───── target */}
                <div className="mt-4">
                  <div className="relative h-2 rounded-full bg-[var(--surface-2)] border border-[var(--line)] overflow-visible">
                    <div className="absolute inset-y-0 rounded-full"
                         style={{
                           left: stopOnLeft ? 0 : `${progress}%`,
                           width: stopOnLeft ? `${progress}%` : `${100 - progress}%`,
                           background: t === "down" ? "var(--down)" : "var(--up)",
                           opacity: .8,
                         }} />
                    {/* entry tick */}
                    <div className="absolute -top-1 -bottom-1 w-[2px] bg-[var(--ink-2)]"
                         style={{ left: `${entryAt}%` }} title={`Entry ${usd(p.entry_price)}`} />
                    {/* current price marker */}
                    <div className="absolute -top-[5px] w-3 h-3 rounded-full border-2 border-[var(--surface)] shadow-sm"
                         style={{
                           left: `calc(${progress}% - 6px)`,
                           background: t === "down" ? "var(--down)" : "var(--up)",
                         }} />
                  </div>
                  <div className="flex justify-between mt-1.5 text-[11px] tnum">
                    <span className="text-[var(--down-ink)]">
                      Stop {usd(stopOnLeft ? p.stop_price : p.target_price)}
                    </span>
                    <span className="text-[var(--up-ink)]">
                      Target {usd(stopOnLeft ? p.target_price : p.stop_price)}
                    </span>
                  </div>
                </div>

                {/* Facts grid */}
                <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-3 mt-4">
                  <Fact label="Entry" value={usd(p.entry_price)} />
                  <Fact label="Mark" value={usd(p.mark_price)} />
                  <Fact label="To stop"
                        value={p.to_stop_pct != null ? `${p.to_stop_pct.toFixed(2)}%` : "—"}
                        toneClass="text-[var(--down-ink)]" />
                  <Fact label="To target"
                        value={p.to_target_pct != null ? `${p.to_target_pct.toFixed(2)}%` : "—"}
                        toneClass="text-[var(--up-ink)]" />
                </dl>

                <div className="flex items-center justify-between mt-3 pt-3 border-t border-[var(--line)] text-[11px] text-[var(--ink-3)] tnum">
                  <span>Held {duration(p.held_minutes)} of {duration(p.max_hold_minutes)} max</span>
                  <span>Peak {p.peak_pct >= 0 ? "+" : "−"}{Math.abs(p.peak_pct).toFixed(2)}%</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function Fact({ label, value, toneClass = "text-[var(--ink)]" }: {
  label: string; value: string; toneClass?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="label truncate">{label}</dt>
      <dd className={`text-sm font-semibold tnum mt-0.5 truncate ${toneClass}`}>{value}</dd>
    </div>
  );
}
