"use client";
import { useState } from "react";
import { Card, Empty, Pill, Signed } from "./ui";
import { clockTime, usd } from "../lib/format";

export type ShadowTrade = {
  id: string; entry_ts: string; strategy: string; symbol: string; side: string;
  entry_px: number; width_pct: number; size_mult: number;
  status: "open" | "closed"; peak_pct: number;
  exit_ts?: string; exit_px?: number; pnl_pct?: number;
  held_minutes?: number; exit_reason?: string;
};

export type ShadowSummary = {
  open: number; closed: number; wins: number; losses: number;
  win_rate: number; total_pct: number; avg_win_pct: number; avg_loss_pct: number;
};

export type MissedSignal = {
  id: string; ts: string; strategy: string; symbol: string;
  side: string; width_pct: number; reason: string; detail: string;
};

type Tab = "shadow" | "missed";

const REASON_LABEL: Record<string, string> = {
  wallet_empty:   "Wallet empty",
  order_failed:   "Order rejected",
  zero_contracts: "Size rounded to zero",
  kill_switch:    "Kill switch active",
  no_mark:        "No price available",
};

export default function ActivityPanel({ shadowTrades, shadowSummary, missedSignals }: {
  shadowTrades: ShadowTrade[];
  shadowSummary?: ShadowSummary;
  missedSignals: MissedSignal[];
}) {
  const [tab, setTab] = useState<Tab>(missedSignals.length > 0 && shadowTrades.length === 0 ? "missed" : "shadow");

  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: "shadow", label: "Paper trades", count: shadowTrades.length },
    { id: "missed", label: "Missed entries", count: missedSignals.length },
  ];

  return (
    <Card>
      <div className="flex items-center gap-1 px-2 sm:px-3 pt-2 border-b border-[var(--line)] overflow-x-auto scroll-x"
           role="tablist" aria-label="Activity">
        {tabs.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id}
                  onClick={() => setTab(t.id)}
                  className={`px-3 py-2 text-[13px] font-semibold whitespace-nowrap border-b-2 -mb-px transition-colors ${
                    tab === t.id
                      ? "border-[var(--brand)] text-[var(--ink)]"
                      : "border-transparent text-[var(--ink-3)] hover:text-[var(--ink-2)]"
                  }`}>
            {t.label}
            {t.count > 0 && (
              <span className="ml-1.5 text-[11px] tnum text-[var(--ink-3)]">{t.count}</span>
            )}
          </button>
        ))}
      </div>

      {tab === "shadow" && <ShadowView trades={shadowTrades} summary={shadowSummary} />}
      {tab === "missed" && <MissedView rows={missedSignals} />}
    </Card>
  );
}

/* ── Paper / shadow trades ────────────────────────────────────────────────── */

function ShadowView({ trades, summary }: { trades: ShadowTrade[]; summary?: ShadowSummary }) {
  if (trades.length === 0) {
    return <Empty icon="◇" title="No paper trades yet"
                  hint="When a signal fires but can't reach the exchange, it's tracked here through its full stop / target lifecycle." />;
  }
  const recent = [...trades].reverse();

  return (
    <>
      {summary && summary.closed > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 card-pad border-b border-[var(--line)]">
          <Mini label="Closed" value={`${summary.closed}`} sub={`${summary.wins}W / ${summary.losses}L`} />
          <Mini label="Win rate" value={`${summary.win_rate.toFixed(0)}%`} />
          <Mini label="Avg win / loss"
                value={`${summary.avg_win_pct.toFixed(1)}% / ${summary.avg_loss_pct.toFixed(1)}%`} />
          <Mini label="Cumulative" value={<Signed value={summary.total_pct} />} />
        </div>
      )}

      {/* Desktop table */}
      <div className="hidden sm:block scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th>Time</th><th>Symbol</th><th>Side</th>
              <th className="text-right">Entry</th><th>Status</th><th className="text-right">P&L</th>
            </tr>
          </thead>
          <tbody>
            {recent.slice(0, 12).map((t) => (
              <tr key={t.id}>
                <td className="tnum whitespace-nowrap">{clockTime(t.entry_ts)}</td>
                <td className="text-[var(--ink)] font-medium">{t.symbol}</td>
                <td><SideTag side={t.side} /></td>
                <td className="text-right tnum">{usd(t.entry_px)}</td>
                <td>
                  {t.status === "open"
                    ? <Pill tone="brand">Open</Pill>
                    : <Pill tone="neutral">{t.exit_reason ?? "closed"}</Pill>}
                </td>
                <td className="text-right">
                  {t.status === "closed" ? <Signed value={t.pnl_pct} /> : <span className="text-[var(--ink-3)]">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile cards — a 6-column table is unusable on a phone */}
      <ul className="sm:hidden divide-y divide-[var(--line)]">
        {recent.slice(0, 12).map((t) => (
          <li key={t.id} className="px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-semibold text-[var(--ink)] text-sm">{t.symbol}</span>
                <SideTag side={t.side} />
              </div>
              {t.status === "closed"
                ? <Signed value={t.pnl_pct} className="text-sm" />
                : <Pill tone="brand">Open</Pill>}
            </div>
            <p className="text-[11px] text-[var(--ink-3)] mt-1 tnum">
              {clockTime(t.entry_ts)} · entry {usd(t.entry_px)}
              {t.status === "closed" && t.exit_reason ? ` · ${t.exit_reason}` : ""}
            </p>
          </li>
        ))}
      </ul>
    </>
  );
}

/* ── Missed entries ───────────────────────────────────────────────────────── */

function MissedView({ rows }: { rows: MissedSignal[] }) {
  if (rows.length === 0) {
    return <Empty icon="✓" title="Nothing missed"
                  hint="Every signal that crossed the entry gate reached the exchange." />;
  }
  const recent = [...rows].reverse().slice(0, 12);

  return (
    <ul className="divide-y divide-[var(--line)]">
      {recent.map((m) => (
        <li key={m.id} className="px-4 py-3">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2 min-w-0">
              <span className="font-semibold text-[var(--ink)] text-sm">{m.symbol || m.strategy}</span>
              {m.side && <SideTag side={m.side} />}
            </div>
            <Pill tone="warn">{REASON_LABEL[m.reason] ?? m.reason}</Pill>
          </div>
          <p className="text-[11px] text-[var(--ink-3)] mt-1 tnum break-words">
            {clockTime(m.ts)}
            {m.detail ? ` · ${m.detail}` : ""}
          </p>
        </li>
      ))}
    </ul>
  );
}

/* ── Bits ─────────────────────────────────────────────────────────────────── */

function SideTag({ side }: { side: string }) {
  const long = side === "buy";
  return (
    <Pill tone={long ? "up" : "down"}>
      <span aria-hidden="true">{long ? "▲" : "▼"}</span>{long ? "LONG" : "SHORT"}
    </Pill>
  );
}

function Mini({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="min-w-0">
      <p className="label truncate">{label}</p>
      <p className="text-sm font-semibold text-[var(--ink)] mt-0.5 tnum truncate">{value}</p>
      {sub && <p className="text-[11px] text-[var(--ink-3)] tnum">{sub}</p>}
    </div>
  );
}
