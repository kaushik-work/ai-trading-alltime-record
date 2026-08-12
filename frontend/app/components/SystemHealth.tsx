"use client";
import { useEffect, useState } from "react";
import { API, authHeaders } from "../lib/format";
import { Card } from "./ui";

/* Is the council alive and working, right now?
 *
 * The check this panel exists for is DECISION AGE. A council that has stopped
 * deciding looks identical to one that is deciding to stand aside — same green
 * containers, same clear sentinel, same everything. The only difference is a
 * timestamp getting older, which a human never notices watching a log scroll.
 *
 * So the age is rendered large, counts up on its own between polls, and turns
 * red without needing the server to say so. If this component's own fetch dies,
 * the number keeps climbing and goes red — a monitor that fails silent is worse
 * than no monitor, because it reads as "fine".
 *
 * COLOURS COME FROM THE APP'S TOKENS, NOT FROM HEX. The first version hardcoded
 * a dark palette (#7d8896 on #0d1117) into a dashboard that renders light, so
 * every label was pale grey on white and every separator was a hard dark rule.
 * It was unreadable in exactly the theme it shipped in. Tokens also mean this
 * follows the rest of the app if the theme ever changes.
 */

type Check = { name: string; state: "ok" | "warn" | "fail"; detail: string; value?: unknown };
type Health = {
  overall: "ok" | "warn" | "fail";
  checked_at: string;
  last_decision_at: string | null;
  market_open?: boolean;
  checks: Check[];
};

const TONE: Record<Check["state"], { fg: string; wash: string; mark: string }> = {
  ok:   { fg: "var(--up)",    wash: "var(--up-wash, rgba(34,150,83,0.08))",   mark: "●" },
  warn: { fg: "var(--brand)", wash: "var(--brand-wash, rgba(210,153,34,0.10))", mark: "▲" },
  fail: { fg: "var(--down)",  wash: "var(--down-wash, rgba(200,50,50,0.10))",  mark: "✕" },
};

function age(iso: string | null, nowMs: number): number | null {
  if (!iso) return null;
  return Math.max(0, Math.round((nowMs - new Date(iso).getTime()) / 1000));
}

/** Seconds as something a human reads at a glance, not a five-digit number. */
function humanAge(secs: number | null): string {
  if (secs === null) return "—";
  if (secs < 90) return `${secs}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  return `${(secs / 3600).toFixed(1)}h`;
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
  // is correct and must not read as an alarm.
  const trading = h?.market_open !== false;
  const stale = trading && (secs === null ? true : secs >= 120);
  const overall: Check["state"] =
    err || !h ? "fail" : stale && h.overall === "ok" ? "warn" : h.overall;
  const tone = TONE[overall];

  // Rows the operator scans for are worth pulling out of alphabetical order.
  const checks = h?.checks ?? [];

  return (
    <Card>
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "14px 16px 12px", borderBottom: "1px solid var(--line)",
      }}>
        <span style={{ color: tone.fg, fontSize: 16, lineHeight: 1 }}>{tone.mark}</span>
        <strong style={{ fontSize: 14 }}>Council health</strong>
        <span style={{
          background: tone.wash, color: tone.fg, borderRadius: "var(--r-sm, 4px)",
          padding: "2px 8px", fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
        }}>
          {overall.toUpperCase()}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
          polled {humanAge(age(h?.checked_at ?? null, now))} ago
        </span>
      </div>

      {/* Decision age. The number that tells you it is alive. */}
      <div style={{
        display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap",
        padding: "16px", background: tone.wash,
      }}>
        <span style={{
          fontSize: 30, fontWeight: 700, lineHeight: 1,
          fontVariantNumeric: "tabular-nums", color: tone.fg,
        }}>
          {humanAge(secs)}
        </span>
        <span style={{ fontSize: 13, color: "var(--ink-2)" }}>
          since the last decision
          {!trading && " — idle, market closed"}
          {trading && secs !== null && secs >= 120 && " — the council may have stopped"}
        </span>
      </div>

      {err && (
        <div style={{
          padding: "10px 16px", fontSize: 12,
          color: "var(--down)", background: "var(--down-wash, transparent)",
        }}>
          health endpoint unreachable: {err}
        </div>
      )}

      <div>
        {checks.map((c, i) => {
          const t = TONE[c.state] ?? TONE.warn;
          return (
            <div key={c.name} style={{
              display: "grid",
              gridTemplateColumns: "18px minmax(120px, 168px) 1fr",
              alignItems: "baseline", gap: 10,
              padding: "9px 16px",
              fontSize: 12.5,
              borderTop: i === 0 ? "none" : "1px solid var(--line)",
              background: c.state === "ok" ? "transparent" : t.wash,
            }}>
              <span style={{ color: t.fg, fontSize: 10, lineHeight: 1.6 }}>{t.mark}</span>
              <span style={{ color: "var(--ink-2)" }}>{c.name}</span>
              <span style={{
                color: c.state === "ok" ? "var(--ink)" : t.fg,
                fontWeight: c.state === "ok" ? 400 : 600,
                wordBreak: "break-word",
              }}>
                {c.detail}
              </span>
            </div>
          );
        })}
        {!h && !err && (
          <div style={{ padding: "12px 16px", fontSize: 12, color: "var(--ink-3)" }}>
            loading…
          </div>
        )}
      </div>
    </Card>
  );
}
