"use client";
import { useEffect, useState } from "react";
import { API, authHeaders } from "../lib/format";

/* Left rail: one entry per instrument, plus the venue overviews.
 *
 * Replaces the two-way Crypto/NSE switch, which hid half the system behind a
 * toggle and gave no room to grow past two venues.
 *
 * A TAB SAYS WHAT IT CAN DO, NOT JUST ITS NAME. Four of the six instruments
 * cannot currently trade — FINNIFTY and BANKNIFTY are refused on measured
 * spread grounds, BTC and ETH have no lens wired yet — and a rail of six
 * identical-looking tabs would imply six working bots. Each carries a status
 * dot and a one-word reason instead, so the sidebar tells you the truth about
 * coverage before you click anything.
 */

export type TabKey =
  | "crypto" | "nse"
  | "NIFTY" | "BANKNIFTY" | "SENSEX" | "FINNIFTY"
  | "BTC" | "ETH" | "XAUT";

type Status = "live" | "blocked" | "none";

type Entry = {
  key: TabKey;
  label: string;
  group: "Venues" | "NSE / BSE" | "Crypto";
  status: Status;
  note: string;
};

/* Status is a claim about the SYSTEM, not a style choice:
 *   live    — the council runs it and it may place orders
 *   blocked — deliberately refused, with a measured reason
 *   none    — nothing wired yet; the tab is a placeholder, and says so
 */
const ENTRIES: Entry[] = [
  { key: "nse",    label: "NSE overview",  group: "Venues", status: "live",    note: "council" },
  { key: "crypto", label: "Crypto overview", group: "Venues", status: "none", note: "no lens yet" },

  { key: "NIFTY",     label: "NIFTY",     group: "NSE / BSE", status: "live",    note: "measured" },
  { key: "SENSEX",    label: "SENSEX",    group: "NSE / BSE", status: "live",    note: "spread ok" },
  { key: "BANKNIFTY", label: "BANKNIFTY", group: "NSE / BSE", status: "live",    note: "spread ok" },
  { key: "FINNIFTY",  label: "FINNIFTY",  group: "NSE / BSE", status: "blocked", note: "1.79% spread" },

  { key: "BTC",  label: "BTC",  group: "Crypto", status: "none", note: "measuring" },
  { key: "ETH",  label: "ETH",  group: "Crypto", status: "none", note: "measuring" },
  { key: "XAUT", label: "XAUT", group: "Crypto", status: "none", note: "4mo history" },
];

const DOT: Record<Status, string> = {
  live: "#3fb950",
  blocked: "#d29922",
  none: "#7d8896",
};

export default function SideTabs({
  active, onChange,
}: { active: TabKey; onChange: (k: TabKey) => void }) {
  const [health, setHealth] = useState<"ok" | "warn" | "fail" | null>(null);

  // The rail shows overall health so a red system is visible from any tab,
  // not only from the one that happens to render the health card.
  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${API}/api/nse/health`, { headers: authHeaders() });
        const j = await r.json();
        if (!stop) setHealth(j.overall ?? null);
      } catch { if (!stop) setHealth("fail"); }
    };
    pull();
    const t = window.setInterval(pull, 15000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  const groups: Entry["group"][] = ["Venues", "NSE / BSE", "Crypto"];

  return (
    <nav aria-label="Instruments" style={{
      width: 208, flexShrink: 0, borderRight: "1px solid var(--line)",
      paddingRight: 12, position: "sticky", top: 12, alignSelf: "flex-start",
      maxHeight: "calc(100vh - 24px)", overflowY: "auto",
    }}>
      {health && (
        <div style={{
          display: "flex", alignItems: "center", gap: 6, padding: "6px 8px",
          marginBottom: 10, borderRadius: 6, fontSize: 11,
          background: health === "ok" ? "rgba(63,185,80,0.10)"
                    : health === "warn" ? "rgba(210,153,34,0.10)"
                    : "rgba(248,81,73,0.12)",
          color: health === "ok" ? "#3fb950" : health === "warn" ? "#d29922" : "#f85149",
        }}>
          <span style={{ fontSize: 13 }}>
            {health === "ok" ? "●" : health === "warn" ? "▲" : "✕"}
          </span>
          council {health}
        </div>
      )}

      {groups.map((g) => (
        <div key={g} style={{ marginBottom: 14 }}>
          <div style={{
            fontSize: 10, letterSpacing: 0.6, textTransform: "uppercase",
            color: "var(--ink-3, #7d8896)", padding: "0 8px 4px",
          }}>{g}</div>

          {ENTRIES.filter((e) => e.group === g).map((e) => {
            const on = active === e.key;
            const dim = e.status !== "live";
            return (
              <button
                key={e.key}
                onClick={() => onChange(e.key)}
                aria-current={on ? "page" : undefined}
                title={`${e.label} — ${e.note}`}
                style={{
                  display: "flex", alignItems: "center", gap: 8, width: "100%",
                  textAlign: "left", padding: "6px 8px", marginBottom: 2,
                  borderRadius: 6, border: "none", cursor: "pointer",
                  fontSize: 13,
                  background: on ? "var(--surface-2, rgba(127,127,127,0.12))" : "transparent",
                  color: on ? "var(--ink, inherit)" : dim ? "#8b949e" : "inherit",
                  fontWeight: on ? 600 : 400,
                }}
              >
                <span style={{ color: DOT[e.status], fontSize: 9 }}>●</span>
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden",
                               textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.label}
                </span>
                <span style={{ fontSize: 9, color: "#7d8896", whiteSpace: "nowrap" }}>
                  {e.note}
                </span>
              </button>
            );
          })}
        </div>
      ))}

      <div style={{ fontSize: 10, color: "#7d8896", padding: "8px 8px 0",
                    borderTop: "1px solid var(--line)", lineHeight: 1.5 }}>
        <span style={{ color: DOT.live }}>●</span> trading ·{" "}
        <span style={{ color: DOT.blocked }}>●</span> refused ·{" "}
        <span style={{ color: DOT.none }}>●</span> not wired
      </div>
    </nav>
  );
}
