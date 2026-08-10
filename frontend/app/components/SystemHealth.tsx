"use client";
import { useEffect, useState } from "react";
import { API, authHeaders } from "../lib/format";
import { Card } from "./ui";

/* Is the council alive and working, right now?
 *
 * The check this panel exists for is DECISION AGE. A council that has stopped
 * deciding looks identical to one that is deciding to stand aside — same green
 * containers, same clear sentinel, same everything. The only difference is a
 * timestamp getting older, which is exactly the kind of thing a human never
 * notices by watching a log scroll past.
 *
 * So the age is rendered large, counts up on its own between polls, and turns
 * red without needing the server to say so. If this component's own fetch dies,
 * the number keeps climbing and goes red — a monitor that fails silent is worse
 * than no monitor, because it reads as "fine".
 */

type Check = { name: string; state: "ok" | "warn" | "fail"; detail: string; value?: unknown };
type Health = {
  overall: "ok" | "warn" | "fail";
  checked_at: string;
  last_decision_at: string | null;
  market_open?: boolean;
  checks: Check[];
};

const TONE = {
  ok:   { fg: "#3fb950", bg: "rgba(63,185,80,0.10)", mark: "●" },
  warn: { fg: "#d29922", bg: "rgba(210,153,34,0.10)", mark: "▲" },
  fail: { fg: "#f85149", bg: "rgba(248,81,73,0.12)", mark: "✕" },
} as const;

function age(iso: string | null, nowMs: number): number | null {
  if (!iso) return null;
  return Math.max(0, Math.round((nowMs - new Date(iso).getTime()) / 1000));
}

export default function SystemHealth() {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${API}/api/nse/health`, { headers: authHeaders() });
        const j = await r.json();
        if (stop) return;
        setH(j);
        setErr(null);
      } catch (e: unknown) {
        if (!stop) setErr(e instanceof Error ? e.message : String(e));
      }
    };
    pull();
    const t = window.setInterval(pull, 10000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  // Ticks independently of the fetch, so the age keeps climbing even if the
  // API goes away. That is the failure this panel most needs to show.
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  const secs = age(h?.last_decision_at ?? null, now);
  // Outside market hours the council idles BY DESIGN, so a rising decision age
  // is correct. The panel used to shout "3028s — the council may have stopped"
  // in warning yellow directly above a row reading "idle — market closed",
  // which is the panel arguing with itself and teaching you to trust neither.
  const trading = h?.market_open !== false;
  const stale = trading && (secs === null ? true : secs >= 120);
  const overall: "ok" | "warn" | "fail" =
    err || !h ? "fail" : stale && h.overall === "ok" ? "warn" : h.overall;
  const tone = TONE[overall];

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <span style={{ color: tone.fg, fontSize: 18 }}>{tone.mark}</span>
        <strong>Council health</strong>
        <span style={{
          background: tone.bg, color: tone.fg, borderRadius: 4,
          padding: "1px 8px", fontSize: 11, fontWeight: 600, letterSpacing: 0.4,
        }}>
          {overall.toUpperCase()}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: "#7d8896" }}>
          {h ? `polled ${age(h.checked_at, now) ?? 0}s ago` : "…"}
        </span>
      </div>

      {/* Decision age, large. This is the number that tells you it is alive. */}
      <div style={{
        display: "flex", alignItems: "baseline", gap: 10,
        padding: "10px 12px", borderRadius: 6, background: tone.bg, marginBottom: 10,
      }}>
        <span style={{
          fontSize: 26, fontWeight: 700, fontVariantNumeric: "tabular-nums",
          color: tone.fg,
        }}>
          {secs === null ? "—" : `${secs}s`}
        </span>
        <span style={{ fontSize: 12, color: "#c9d3e0" }}>
          since the last decision
          {!trading && " — idle, market closed"}
          {trading && secs !== null && secs >= 120 &&
            " — the council may have stopped"}
        </span>
      </div>

      {err && (
        <div style={{ color: TONE.fail.fg, fontSize: 12, marginBottom: 8 }}>
          health endpoint unreachable: {err}
        </div>
      )}

      <div style={{ display: "grid", gap: 4 }}>
        {(h?.checks ?? []).map((c) => {
          const t = TONE[c.state] ?? TONE.warn;
          return (
            <div key={c.name} style={{
              display: "flex", alignItems: "baseline", gap: 8, fontSize: 12,
              padding: "3px 0", borderBottom: "1px solid #1b222c",
            }}>
              <span style={{ color: t.fg, width: 12 }}>{t.mark}</span>
              <span style={{ minWidth: 140, color: "#c9d3e0" }}>{c.name}</span>
              <span style={{ color: "#7d8896" }}>{c.detail}</span>
            </div>
          );
        })}
        {!h && !err && <div style={{ fontSize: 12, color: "#7d8896" }}>loading…</div>}
      </div>
    </Card>
  );
}
