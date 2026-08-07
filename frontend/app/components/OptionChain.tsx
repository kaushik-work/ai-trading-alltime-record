"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { WS, clockTime, getToken, isTokenValid, pct } from "../lib/format";
import { Banner, Card, CardHead, Empty, Pill } from "./ui";

/* Classic option-chain layout: CALLS on the left, STRIKE down the centre, PUTS
   on the right. That convention is near-universal across NSE, TradingView, IBKR
   and Indian retail platforms, so traders read it without instruction — this is
   not the place to be inventive.

   Two visual devices do most of the work:

   1. ITM shading. A call is in-the-money BELOW spot and a put ABOVE it, so the
      shaded blocks meet at the ATM row and form a natural centre mark.
   2. Mirrored OI bars. Open interest is drawn as a bar behind the number,
      growing away from the strike column. Relative OI is what traders scan for
      — where the walls are — and a bar answers that far faster than digits.

   Greeks are hidden by default (the table is already wide) and are greyed with
   a warning inside 2 DTE, where analytic Greeks stop being usable. */

type Leg = {
  ltp: number; bid: number; ask: number; mid: number;
  spread: number; spread_pct: number;
  volume: number; oi: number; oi_change_pct: number;
  iv: number; delta: number; gamma: number; theta: number; vega: number;
  book_imbalance: number; tradingsymbol: string | null;
} | null;

type Row = {
  strike: number; is_atm: boolean; ce_itm: boolean; pe_itm: boolean;
  ce: Leg; pe: Leg;
};

type Chain = {
  symbol: string; spot: number; atm: number; step: number; lot_size: number;
  expiry: string; dte: number; greeks_trustworthy: boolean;
  vix: number | null; source: "ws" | "rest";
  rows: Row[];
  totals: { ce_oi: number; pe_oi: number; pcr: number | null; max_pain: number | null };
};

const SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"];

const num = (n: number | null | undefined, d = 2) =>
  n == null || !isFinite(n) || n === 0 ? "—" : n.toFixed(d);

const compact = (n: number | null | undefined) => {
  if (n == null || !isFinite(n) || n === 0) return "—";
  if (Math.abs(n) >= 1e7) return `${(n / 1e7).toFixed(2)}Cr`;
  if (Math.abs(n) >= 1e5) return `${(n / 1e5).toFixed(2)}L`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
};

export default function OptionChain() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [strikes, setStrikes] = useState(10);
  const [showGreeks, setShowGreeks] = useState(false);
  const [chain, setChain] = useState<Chain | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* Tick-driven, not polled. The server pushes at 10Hz and sends only the
     strikes that actually changed, so the table tracks the exchange as closely
     as a browser can — bounded by network round-trip, not by a poll timer. */
  useEffect(() => {
    setChain(null);
    let closed = false;

    const connect = () => {
      const token = getToken();
      if (!isTokenValid(token)) return;
      const url = `${WS}/ws/nse/chain?token=${encodeURIComponent(token || "")}`
        + `&symbol=${symbol}&strikes=${strikes}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => { setLive(true); setError(null); };
      ws.onmessage = (ev) => {
        let msg: any;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "error") { setError(msg.message); return; }
        setUpdated(new Date().toISOString());
        setError(null);
        if (msg.type === "snapshot") {
          setChain(msg as Chain);
        } else if (msg.type === "patch") {
          // Merge changed strikes into the existing ladder rather than
          // replacing it, so untouched rows keep their identity and React
          // does not remount the whole table on every frame.
          setChain((prev) => {
            if (!prev) return prev;
            const byStrike = new Map(prev.rows.map((r) => [r.strike, r]));
            for (const r of (msg.rows as Row[]) ?? []) byStrike.set(r.strike, r);
            const { type: _t, rows: _r, ...header } = msg;
            return { ...prev, ...header,
                     rows: Array.from(byStrike.values())
                                .sort((a, b) => a.strike - b.strike) };
          });
        }
      };
      ws.onclose = () => {
        setLive(false);
        wsRef.current = null;
        if (!closed) retry.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };
    connect();

    return () => {
      closed = true;
      if (retry.current) clearTimeout(retry.current);
      wsRef.current?.close();
    };
  }, [symbol, strikes]);

  // One shared scale for both sides, so a call wall and a put wall of equal
  // size draw equal bars. Scaling each side to its own max would make the
  // quieter side look just as heavy as the busy one.
  const maxOi = useMemo(() => {
    if (!chain) return 1;
    return Math.max(1, ...chain.rows.flatMap((r) => [r.ce?.oi ?? 0, r.pe?.oi ?? 0]));
  }, [chain]);

  const cols = showGreeks ? 7 : 4;

  return (
    <Card>
      <CardHead
        title="Option chain"
        sub={chain
          ? `${chain.symbol} · expiry ${chain.expiry} · ${chain.dte.toFixed(1)} DTE · lot ${chain.lot_size}`
          : "Loading…"}
        right={
          <div className="flex items-center gap-2 flex-wrap justify-end">
            {/* Two independent things, both worth seeing: is THIS browser
                connected, and is the server reading the exchange socket or
                falling back to REST. A green pill with a "rest" source means
                the page is live but the feed underneath it is not. */}
            <Pill tone={live ? (chain?.source === "ws" ? "up" : "warn") : "down"}
                  title={!live ? "Disconnected — retrying every 3s"
                    : chain?.source === "ws"
                      ? "Streaming: server is reading the Angel socket"
                      : "Connected, but the server is on a REST fallback — the "
                        + "exchange socket has no fresh coverage (normal outside market hours)"}>
              {!live ? "○ offline" : chain?.source === "ws" ? "● live" : "◐ rest"}
            </Pill>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
                    aria-label="Underlying" className="btn btn-ghost py-1 px-2 text-xs">
              {SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select value={strikes} onChange={(e) => setStrikes(Number(e.target.value))}
                    aria-label="Strikes each side" className="btn btn-ghost py-1 px-2 text-xs">
              {[5, 10, 15, 20].map((n) => <option key={n} value={n}>±{n}</option>)}
            </select>
            <button onClick={() => setShowGreeks((v) => !v)}
                    aria-pressed={showGreeks}
                    className={`btn py-1 px-2 text-xs ${showGreeks ? "btn-primary" : "btn-ghost"}`}>
              Greeks
            </button>
          </div>
        }
      />

      {error && (
        <div className="card-pad">
          <Banner tone="warn" title="Could not load the option chain" body={error}
                  onDismiss={() => setError(null)} />
        </div>
      )}

      {chain && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 px-4 py-2.5
                        border-b border-[var(--line)] bg-[var(--surface-2)] text-xs">
          <span className="tnum">
            <span className="text-[var(--ink-3)]">Spot </span>
            <span className="font-semibold text-[var(--ink)]">
              {chain.spot.toLocaleString("en-IN")}
            </span>
          </span>
          <span className="tnum">
            <span className="text-[var(--ink-3)]">ATM </span>
            <span className="font-semibold text-[var(--ink)]">{chain.atm}</span>
          </span>
          <span className="tnum" title="Put OI / Call OI. Above 1 means more puts written.">
            <span className="text-[var(--ink-3)]">PCR </span>
            <span className="font-semibold text-[var(--ink)]">{num(chain.totals.pcr, 2)}</span>
          </span>
          <span className="tnum" title="Strike where option writers lose the least">
            <span className="text-[var(--ink-3)]">Max pain </span>
            <span className="font-semibold text-[var(--ink)]">
              {chain.totals.max_pain ?? "—"}
            </span>
          </span>
          {chain.vix != null && (
            <span className="tnum">
              <span className="text-[var(--ink-3)]">VIX </span>
              <span className="font-semibold text-[var(--ink)]">{chain.vix.toFixed(2)}</span>
            </span>
          )}
          {updated && (
            <span className="tnum text-[var(--ink-3)] ml-auto">
              updated {clockTime(updated)}
            </span>
          )}
        </div>
      )}

      {showGreeks && chain && !chain.greeks_trustworthy && (
        <div className="px-4 py-2 text-[11px] leading-snug"
             style={{ background: "var(--warn-wash)", color: "var(--warn-ink)" }}>
          <strong>Greeks unreliable at {chain.dte.toFixed(1)} DTE.</strong> Inside 2 days
          gamma explodes and delta flips violently around the strike; analytic values
          here can be 100% wrong. Shown greyed — do not size off them.
        </div>
      )}

      {!chain && !error ? (
        <div className="card-pad space-y-2">
          {Array.from({ length: 8 }).map((_, i) => <div key={i} className="skel h-7 w-full" />)}
        </div>
      ) : chain && chain.rows.length === 0 ? (
        <Empty icon="○" title="No strikes quoted"
               hint="The chain came back empty. Outside market hours this is expected." />
      ) : chain ? (
        <div className="scroll-x">
          <table className="tbl">
            <thead>
              <tr>
                <th colSpan={cols} className="!text-center"
                    style={{ borderBottom: "2px solid var(--up)" }}>Calls</th>
                <th className="!text-center" style={{ background: "var(--surface)" }}>Strike</th>
                <th colSpan={cols} className="!text-center"
                    style={{ borderBottom: "2px solid var(--down)" }}>Puts</th>
              </tr>
              <tr>
                {/* Calls read right-to-left toward the strike: OI furthest out,
                    price nearest the centre. Mirrored on the put side so both
                    sides put their most-read column closest to the strike. */}
                <th className="text-right">OI</th>
                <th className="text-right">Vol</th>
                {showGreeks && <th className="text-right">IV %</th>}
                {showGreeks && <th className="text-right">Δ</th>}
                {showGreeks && <th className="text-right">Θ</th>}
                <th className="text-right">Bid / Ask</th>
                <th className="text-right">LTP</th>
                <th className="!text-center">—</th>
                <th className="text-left">LTP</th>
                <th className="text-left">Bid / Ask</th>
                {showGreeks && <th className="text-left">Θ</th>}
                {showGreeks && <th className="text-left">Δ</th>}
                {showGreeks && <th className="text-left">IV %</th>}
                <th className="text-left">Vol</th>
                <th className="text-left">OI</th>
              </tr>
            </thead>
            <tbody>
              {chain.rows.map((r) => (
                <ChainRow key={r.strike} row={r} maxOi={maxOi} showGreeks={showGreeks}
                          greeksOk={chain.greeks_trustworthy} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <p className="px-4 py-2.5 text-[11px] text-[var(--ink-3)] leading-relaxed border-t border-[var(--line)]">
        Shaded cells are in-the-money. Bars behind open interest are scaled to the
        largest position on either side. Greeks are computed from the current mark,
        never read from storage.
      </p>
    </Card>
  );
}

/** A price that flashes on change — the standard tick cue in a trading table.
 *
 *  The flash is the only signal here that relies on hue, so it is deliberately
 *  transient decoration on top of a number that is always readable. Direction
 *  is never communicated by the flash alone. */
function Px({ value, className = "" }: { value: number | undefined; className?: string }) {
  const prev = useRef<number | undefined>(undefined);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    const p = prev.current;
    if (p !== undefined && value !== undefined && value !== p && value > 0 && p > 0) {
      setFlash(value > p ? "up" : "down");
      const t = setTimeout(() => setFlash(null), 450);
      return () => clearTimeout(t);
    }
    prev.current = value;
  }, [value]);

  useEffect(() => { prev.current = value; }, [value]);

  return (
    <span className={`tnum transition-colors duration-300 ${className}`}
          style={flash ? {
            background: flash === "up" ? "var(--up-wash)" : "var(--down-wash)",
            color: flash === "up" ? "var(--up-ink)" : "var(--down-ink)",
            borderRadius: "3px", padding: "1px 3px", margin: "-1px -3px",
          } : undefined}>
      {num(value)}
    </span>
  );
}

function ChainRow({ row, maxOi, showGreeks, greeksOk }: {
  row: Row; maxOi: number; showGreeks: boolean; greeksOk: boolean;
}) {
  const atm = row.is_atm;
  const rowStyle = atm
    ? { background: "var(--brand-wash)", outline: "1px solid var(--brand)" }
    : undefined;
  const itm = { background: "var(--warn-wash)" };
  const dim = greeksOk ? "" : "opacity-40";

  return (
    <tr style={rowStyle}>
      {/* ── calls ── */}
      <OiCell leg={row.ce} maxOi={maxOi} side="ce" itm={row.ce_itm} />
      <td className="text-right tnum" style={row.ce_itm ? itm : undefined}>
        {compact(row.ce?.volume)}
      </td>
      {showGreeks && <td className={`text-right tnum ${dim}`} style={row.ce_itm ? itm : undefined}>{num(row.ce?.iv, 1)}</td>}
      {showGreeks && <td className={`text-right tnum ${dim}`} style={row.ce_itm ? itm : undefined}>{num(row.ce?.delta, 2)}</td>}
      {showGreeks && <td className={`text-right tnum ${dim}`} style={row.ce_itm ? itm : undefined}>{num(row.ce?.theta, 1)}</td>}
      <td className="text-right tnum text-[var(--ink-3)] whitespace-nowrap"
          style={row.ce_itm ? itm : undefined}
          title={row.ce ? `spread ${pct(row.ce.spread_pct, 2)}` : undefined}>
        {row.ce ? `${num(row.ce.bid)} / ${num(row.ce.ask)}` : "—"}
      </td>
      <td className="text-right font-semibold text-[var(--ink)]"
          style={row.ce_itm ? itm : undefined}>
        <Px value={row.ce?.ltp} />
      </td>

      {/* ── strike ── */}
      <td className="text-center tnum font-bold text-[var(--ink)] whitespace-nowrap"
          style={{ background: atm ? "var(--brand-wash)" : "var(--surface-2)" }}>
        {row.strike}
        {atm && <span className="ml-1 text-[9px] font-semibold text-[var(--brand-ink)]">ATM</span>}
      </td>

      {/* ── puts ── */}
      <td className="text-left font-semibold text-[var(--ink)]"
          style={row.pe_itm ? itm : undefined}>
        <Px value={row.pe?.ltp} />
      </td>
      <td className="text-left tnum text-[var(--ink-3)] whitespace-nowrap"
          style={row.pe_itm ? itm : undefined}
          title={row.pe ? `spread ${pct(row.pe.spread_pct, 2)}` : undefined}>
        {row.pe ? `${num(row.pe.bid)} / ${num(row.pe.ask)}` : "—"}
      </td>
      {showGreeks && <td className={`text-left tnum ${dim}`} style={row.pe_itm ? itm : undefined}>{num(row.pe?.theta, 1)}</td>}
      {showGreeks && <td className={`text-left tnum ${dim}`} style={row.pe_itm ? itm : undefined}>{num(row.pe?.delta, 2)}</td>}
      {showGreeks && <td className={`text-left tnum ${dim}`} style={row.pe_itm ? itm : undefined}>{num(row.pe?.iv, 1)}</td>}
      <td className="text-left tnum" style={row.pe_itm ? itm : undefined}>
        {compact(row.pe?.volume)}
      </td>
      <OiCell leg={row.pe} maxOi={maxOi} side="pe" itm={row.pe_itm} />
    </tr>
  );
}

/** Open interest with a bar growing away from the strike column. */
function OiCell({ leg, maxOi, side, itm }: {
  leg: Leg; maxOi: number; side: "ce" | "pe"; itm: boolean;
}) {
  const oi = leg?.oi ?? 0;
  const w = Math.min(100, (oi / maxOi) * 100);
  const isCall = side === "ce";
  const bar = isCall ? "var(--up-wash)" : "var(--down-wash)";
  const chg = leg?.oi_change_pct ?? 0;

  return (
    <td className={`relative tnum ${isCall ? "text-right" : "text-left"}`}
        style={itm ? { background: "var(--warn-wash)" } : undefined}
        title={oi ? `OI ${oi.toLocaleString("en-IN")}${chg ? ` · ${chg > 0 ? "+" : "−"}${Math.abs(chg).toFixed(1)}%` : ""}` : undefined}>
      {/* Bar sits behind the digits and is decorative — the number is the data,
          so a screen reader gets the value either way. */}
      <span aria-hidden="true" className="absolute inset-y-0.5 block rounded-[2px]"
            style={{ width: `${w}%`, background: bar, [isCall ? "right" : "left"]: 0 }} />
      <span className="relative">
        {compact(oi)}
        {chg !== 0 && (
          <span className={`ml-1 text-[10px] ${chg > 0 ? "text-[var(--up-ink)]" : "text-[var(--down-ink)]"}`}>
            {chg > 0 ? "▲" : "▼"}
          </span>
        )}
      </span>
    </td>
  );
}
