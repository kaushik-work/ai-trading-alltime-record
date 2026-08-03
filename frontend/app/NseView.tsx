"use client";
import { useEffect, useState } from "react";
import { Banner, Card, CardHead, Empty, Pill, Stat } from "./components/ui";
import { API, authHeaders, getToken, inr } from "./lib/format";

type NseLeg = { side: string; type: string; strike: number; lots: number; filled_px: number | null };

type NsePosition = {
  position_id: string; symbol: string; side: string; entry_time: string;
  pred_pct: number; spot_at_entry: number; max_hold_until: string; legs: NseLeg[];
};

type NseState = {
  enabled: boolean; mode: string; killed: boolean;
  day_pnl: number; unrealized_pnl: number;
  total_capital: number; margin_used: number; margin_available: number;
  broker_rms?: { available_cash?: number; available_limit?: number; net?: number; utiliseddebits?: number };
  open_positions: NsePosition[]; journal_count: number;
};

export default function NseView() {
  const [state, setState] = useState<NseState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [killBusy, setKillBusy] = useState(false);

  async function fetchStatus() {
    if (!getToken()) return;
    try {
      const r = await fetch(`${API}/api/nse/status`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`Server returned ${r.status}`);
      setState(await r.json());
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Could not load NSE status");
    }
  }

  async function toggleKill() {
    if (!state) return;
    setKillBusy(true);
    try {
      const r = await fetch(`${API}${state.killed ? "/api/nse/unkill" : "/api/nse/kill"}`,
                            { method: "POST", headers: authHeaders() });
      if (!r.ok) throw new Error(`Server returned ${r.status}`);
      await fetchStatus();
    } catch (e: any) {
      setError(e?.message || "Kill switch failed");
    } finally {
      setKillBusy(false);
    }
  }

  useEffect(() => {
    fetchStatus();
    // Pause polling when the tab is hidden — no point burning the broker's
    // rate limit against a screen nobody is looking at.
    const id = setInterval(() => {
      if (document.visibilityState === "visible") fetchStatus();
    }, 5000);
    return () => clearInterval(id);
  }, []);

  const total = state ? state.day_pnl + (state.unrealized_pnl || 0) : 0;

  return (
    <div className="space-y-4 sm:space-y-6">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-xl sm:text-2xl font-semibold text-[var(--ink)]">NSE · Synthetic forward</h1>
          <p className="text-xs text-[var(--ink-3)] mt-1">
            Angel One SmartAPI · NIFTY / BANKNIFTY / FINNIFTY (close 15:40) · SENSEX (close 15:30)
          </p>
        </div>
        <button onClick={toggleKill} disabled={killBusy || !state}
                className={`btn ${state?.killed ? "btn-ghost" : "btn-danger"}`}>
          {killBusy ? "Working…" : state?.killed ? "Resume NSE" : "Stop NSE"}
        </button>
      </div>

      {error && <Banner tone="warn" title="Could not reach the NSE runner" body={error} onDismiss={() => setError(null)} />}
      {state?.killed && <Banner tone="warn" title="NSE runner is halted" body="No new entries will be taken until you resume it." />}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Runner" value={state ? (state.enabled ? "On" : "Off") : "—"} loading={!state} />
        <Stat label="Day P&L" value={state ? inr(state.day_pnl) : "—"} loading={!state} />
        <Stat label="Unrealized" value={state ? inr(state.unrealized_pnl || 0) : "—"} loading={!state} />
        <Stat label="Total P&L" emphasis value={state ? inr(total) : "—"} loading={!state} />
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Margin used" value={state ? inr(state.margin_used) : "—"} loading={!state} />
        <Stat label="Margin available" value={state ? inr(state.margin_available) : "—"} loading={!state} />
        <Stat label="Broker cash" value={inr(state?.broker_rms?.available_cash)} loading={!state}
              footnote="Live Angel One RMS" />
        <Stat label="Capital pool" value={inr(state?.total_capital)} loading={!state} />
      </div>

      <Card>
        <CardHead title="Open positions"
                  right={<Pill tone={state?.open_positions.length ? "brand" : "neutral"}>
                    {state?.open_positions.length ?? 0} live
                  </Pill>} />
        {!state ? (
          <div className="card-pad"><div className="skel h-16 w-full" /></div>
        ) : state.open_positions.length === 0 ? (
          <Empty icon="○" title="No open NSE positions" hint="The runner enters when the synthetic-forward gate is crossed." />
        ) : (
          <>
            <div className="hidden sm:block scroll-x">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Symbol</th><th>Side</th><th>Entry</th>
                    <th className="text-right">Spot at entry</th><th className="text-right">Pred</th><th>Legs</th>
                  </tr>
                </thead>
                <tbody>
                  {state.open_positions.map((p) => (
                    <tr key={p.position_id}>
                      <td className="font-medium text-[var(--ink)]">{p.symbol}</td>
                      <td>
                        <Pill tone={p.side === "long" ? "up" : "down"}>
                          <span aria-hidden="true">{p.side === "long" ? "▲" : "▼"}</span>
                          {p.side.toUpperCase()}
                        </Pill>
                      </td>
                      <td className="tnum whitespace-nowrap">{new Date(p.entry_time).toLocaleString()}</td>
                      <td className="text-right tnum">{p.spot_at_entry.toLocaleString()}</td>
                      <td className="text-right tnum">{p.pred_pct.toFixed(3)}%</td>
                      <td className="text-[var(--ink-3)]">{p.legs.map((l) => `${l.side} ${l.strike}${l.type}`).join(" / ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="sm:hidden divide-y divide-[var(--line)]">
              {state.open_positions.map((p) => (
                <li key={p.position_id} className="px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-[var(--ink)] text-sm">{p.symbol}</span>
                    <Pill tone={p.side === "long" ? "up" : "down"}>
                      <span aria-hidden="true">{p.side === "long" ? "▲" : "▼"}</span>{p.side.toUpperCase()}
                    </Pill>
                  </div>
                  <p className="text-[11px] text-[var(--ink-3)] mt-1 tnum">
                    spot {p.spot_at_entry.toLocaleString()} · pred {p.pred_pct.toFixed(3)}%
                  </p>
                  <p className="text-[11px] text-[var(--ink-3)] mt-0.5">
                    {p.legs.map((l) => `${l.side} ${l.strike}${l.type}`).join(" / ")}
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>

      <p className="text-xs text-[var(--ink-3)] leading-relaxed">
        Day P&L and margin used are the runner&apos;s internal accounting. Broker cash
        is the live Angel One RMS value.
      </p>
    </div>
  );
}
