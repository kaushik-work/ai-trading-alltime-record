"use client";
import { useEffect, useState } from "react";
import { API, authHeaders } from "../lib/format";
import { Card } from "./ui";

/* Every lens, what it measured, and whether it may touch money.
 *
 * This is the table the whole apparatus exists to produce, and until now it
 * lived only in a Python dict and a markdown file. A dashboard that shows what
 * the system is DOING without showing what it KNOWS invites the assumption
 * that a lens on screen is a lens that works.
 *
 * The count is the honest headline: twelve built, one carrying weight. Sorting
 * weighted lenses first and dimming the rest makes that visible in one glance
 * rather than requiring the reader to scan a column of near-zero numbers.
 */

type Lens = {
  lens: string;
  lifecycle: string;
  weight: number;
  health: number;
  is_dying: boolean;
  n_closed: number;
  trades_until_review: number;
  abstain_rate: number;
  train_bps: number | null;
  validate_bps: number | null;
  note: string;
};

function bps(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}

export default function LensTable() {
  const [rows, setRows] = useState<Lens[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${API}/api/nse/lenses`, { headers: authHeaders() });
        const j = await r.json();
        if (!stop) { setRows(j.lenses ?? []); setErr(j.error ?? null); }
      } catch (e: unknown) {
        if (!stop) setErr(e instanceof Error ? e.message : String(e));
      }
    };
    pull();
    const t = window.setInterval(pull, 30000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  // Weighted first, then by how close to an edge they got. A reader scanning
  // top-down should hit everything that matters before the nulls.
  const sorted = [...rows].sort((a, b) =>
    (b.weight - a.weight) ||
    ((b.validate_bps ?? -99) - (a.validate_bps ?? -99)));

  const weighted = rows.filter((r) => r.weight > 0).length;

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10,
                    padding: "14px 16px 12px",
                    borderBottom: "1px solid var(--line)" }}>
        <strong style={{ fontSize: 14 }}>Lenses</strong>
        <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
          {rows.length} built · <b style={{ color: "var(--ink-2)" }}>{weighted} carrying
          weight</b> · edge in bps, TRAIN / VALIDATE
        </span>
      </div>

      {err && (
        <div style={{ padding: "10px 16px", fontSize: 12, color: "var(--down)" }}>
          {err}
        </div>
      )}

      <div style={{
        display: "grid",
        gridTemplateColumns: "minmax(120px,1fr) 84px 62px 62px 74px 56px",
        gap: 8, padding: "8px 16px", fontSize: 10,
        letterSpacing: 0.4, textTransform: "uppercase",
        color: "var(--ink-3)", borderBottom: "1px solid var(--line)",
      }}>
        <span>lens</span><span>state</span>
        <span style={{ textAlign: "right" }}>train</span>
        <span style={{ textAlign: "right" }}>validate</span>
        <span style={{ textAlign: "right" }}>weight</span>
        <span style={{ textAlign: "right" }}>closed</span>
      </div>

      <div>
        {sorted.map((l) => {
          const live = l.weight > 0;
          const isOpen = open === l.lens;
          return (
            <div key={l.lens}>
              <div
                onClick={() => setOpen(isOpen ? null : l.lens)}
                style={{
                  display: "grid",
                  gridTemplateColumns: "minmax(120px,1fr) 84px 62px 62px 74px 56px",
                  gap: 8, padding: "9px 16px", fontSize: 12.5, cursor: "pointer",
                  alignItems: "baseline",
                  borderBottom: "1px solid var(--line)",
                  background: live ? "var(--up-wash, rgba(34,150,83,0.06))" : "transparent",
                  color: live ? "var(--ink)" : "var(--ink-2)",
                }}
              >
                <span style={{ fontWeight: live ? 600 : 400 }}>
                  {l.lens}{l.is_dying ? " ⚠" : ""}
                </span>
                <span style={{ fontSize: 10, color: "var(--ink-3)" }}>
                  {l.lifecycle}
                </span>
                <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                               color: (l.train_bps ?? 0) > 0 ? "var(--up)"
                                    : l.train_bps === null ? "var(--ink-3)" : "var(--down)" }}>
                  {bps(l.train_bps)}
                </span>
                <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                               color: (l.validate_bps ?? 0) > 0 ? "var(--up)"
                                    : l.validate_bps === null ? "var(--ink-3)" : "var(--down)" }}>
                  {bps(l.validate_bps)}
                </span>
                <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                               fontWeight: live ? 700 : 400 }}>
                  {l.weight.toFixed(2)}
                </span>
                <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                               color: "var(--ink-3)" }}>
                  {l.n_closed}
                </span>
              </div>
              {isOpen && l.note && (
                <div style={{ padding: "10px 16px 12px", fontSize: 11.5,
                              lineHeight: 1.6, color: "var(--ink-2)",
                              background: "var(--surface-2)",
                              borderBottom: "1px solid var(--line)" }}>
                  {l.note}
                </div>
              )}
            </div>
          );
        })}
        {rows.length === 0 && !err && (
          <div style={{ padding: "12px 16px", fontSize: 12, color: "var(--ink-3)" }}>
            loading…
          </div>
        )}
      </div>

      <div style={{ padding: "10px 16px", fontSize: 11, color: "var(--ink-3)",
                    lineHeight: 1.6 }}>
        <b>closed</b> is live trades this lens has voted on. Every lens is at 0,
        so no weight has yet moved on live evidence — 30 closed trades is the
        threshold. Until then the weights come from backtest alone.
      </div>
    </Card>
  );
}
