"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";
import { Banner, Card, CardHead, Empty, Pill, Toggle } from "../components/ui";
import TestOrderCard from "../components/TestOrderCard";
import { API, authHeaders, clearToken, getToken, isTokenValid, usd } from "../lib/format";

type Instrument = { name: string; enabled: boolean };
type Strategy = { name: string; enabled: boolean; instruments: Instrument[] };

function titleize(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function instrumentLabel(name: string): string {
  return name.replace("USD", "").toUpperCase();
}

export default function ControlsPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});


  function logoutAndLogin() {
    clearToken();
    router.replace("/login");
  }

  async function fetchStrategies() {
    if (!isTokenValid(getToken())) { logoutAndLogin(); return; }
    try {
      const r = await fetch(`${API}/api/crypto/strategies`, { headers: authHeaders() });
      if (r.status === 401) { logoutAndLogin(); return; }
      if (!r.ok) throw new Error(`Server returned ${r.status}`);
      const data = await r.json();
      setStrategies(data.strategies ?? []);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Could not load strategies");
    } finally {
      setLoading(false);
    }
  }

  async function toggle(url: string, key: string) {
    if (!isTokenValid(getToken())) { logoutAndLogin(); return; }
    setBusy((b) => ({ ...b, [key]: true }));
    try {
      const r = await fetch(url, { method: "POST", headers: authHeaders() });
      if (r.status === 401) { logoutAndLogin(); return; }
      if (!r.ok) throw new Error(`Server returned ${r.status}`);
      await fetchStrategies();
    } catch (e: any) {
      setError(e?.message || "Could not apply that change");
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }


  useEffect(() => {
    if (!isTokenValid(getToken())) logoutAndLogin();
    else { setAuthed(true); fetchStrategies(); }
  }, []);

  if (!authed) return null;

  return (
    <div className="min-h-screen bg-[var(--plane)]">
      <Header />
      <main className="max-w-3xl mx-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4 sm:space-y-6">
        <div>
          {/* The logo was the only way back, which is not discoverable. */}
          <button onClick={() => router.push("/")}
                  className="inline-flex items-center gap-1.5 text-[13px] font-semibold text-[var(--brand-ink)] hover:underline mb-3">
            <span aria-hidden="true">←</span> Back to dashboard
          </button>
          <h1 className="text-xl sm:text-2xl font-semibold text-[var(--ink)]">Controls</h1>
          <p className="text-sm text-[var(--ink-2)] mt-1 leading-relaxed">
            Turn strategies and instruments on or off. Disabling stops new entries —
            positions already open are still managed until they close.
          </p>
        </div>

        {error && <Banner tone="warn" title="Something went wrong" body={error} onDismiss={() => setError(null)} />}

        {loading ? (
          <Card><div className="card-pad space-y-3"><div className="skel h-6 w-40" /><div className="skel h-16 w-full" /></div></Card>
        ) : strategies.length === 0 ? (
          <Card><Empty icon="◇" title="No strategies configured" hint="The runner has not registered any strategies yet." /></Card>
        ) : (
          strategies.map((s) => {
            const sKey = `strategy:${s.name}`;
            return (
              <Card key={s.name}>
                <CardHead
                  title={titleize(s.name)}
                  right={
                    <>
                      <Pill tone={s.enabled ? "up" : "neutral"}>{s.enabled ? "Enabled" : "Disabled"}</Pill>
                      <Toggle checked={s.enabled} disabled={busy[sKey]} ariaLabel={`Toggle ${s.name}`}
                              onChange={(v) => toggle(`${API}/api/crypto/strategies/${s.name}/${v ? "enable" : "disable"}`, sKey)} />
                    </>
                  }
                />
                {s.instruments.length > 0 && (
                  <div className="card-pad">
                    <p className="label mb-2.5">Instruments</p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                      {s.instruments.map((inst) => {
                        const iKey = `instrument:${s.name}:${inst.name}`;
                        const off = !s.enabled;
                        return (
                          <div key={inst.name}
                               className={`flex items-center justify-between gap-3 rounded-[var(--r-sm)] border border-[var(--line)] px-3 py-2.5 ${
                                 off ? "opacity-55 bg-[var(--surface-2)]" : "bg-[var(--surface)]"}`}>
                            <div className="min-w-0">
                              <p className="text-sm font-semibold text-[var(--ink)]">{instrumentLabel(inst.name)}</p>
                              <p className="text-[11px] text-[var(--ink-3)]">
                                {off ? "Strategy disabled" : inst.enabled ? "Active" : "Paused"}
                              </p>
                            </div>
                            <Toggle size="sm" checked={inst.enabled && s.enabled} disabled={busy[iKey] || off}
                                    ariaLabel={`Toggle ${inst.name}`}
                                    onChange={(v) => toggle(`${API}/api/crypto/strategies/${s.name}/instruments/${inst.name}/${v ? "enable" : "disable"}`, iKey)} />
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </Card>
            );
          })
        )}

        {/* ── Danger zone — real orders on both venues ─────────────────────
            These used to sit on the dashboards, one misclick from the kill
            switch. Two-step confirm, and the exchange's own error is shown
            verbatim: seeing WHY it was rejected is the point. ───────────── */}
        <div>
          <h2 className="text-base font-semibold text-[var(--ink)] mb-1">Danger zone</h2>
          <p className="text-[13px] text-[var(--ink-2)] mb-3">
            Connectivity checks that place genuine orders. Each opens a real
            position you must close yourself.
          </p>
        </div>

        <TestOrderCard
          title="Test order · Delta (crypto)"
          endpoint="/api/crypto/test_buy_btc"
          description="Live market buy for 1 BTCUSD contract at 200× leverage, to verify Delta REST auth, IP whitelisting and trade permissions. Needs only ~$0.32 of margin, but it will fail if the wallet is empty."
          confirmLabel="Confirm: place a real 200× leveraged BTCUSD buy?"
          renderSuccess={(d) => (
            <>
              <p>{d.symbol} {String(d.side || "").toUpperCase()} · size {d.size} · {d.leverage}×</p>
              <p>Mark {usd(d.mark_price)}</p>
            </>
          )}
        />

        <TestOrderCard
          title="Test order · Angel One (NSE)"
          endpoint="/api/nse/test_buy_ce"
          description="Live LIMIT buy for 1 lot of the nearest NIFTY CE at the current ask, with a protective OCO GTT attached. Verifies Angel session, instrument lookup and order permissions."
          confirmLabel="Confirm: place a real NIFTY CE buy for 1 lot?"
          renderSuccess={(d) => (
            <>
              <p>{d.tradingsymbol} · qty {d.quantity}</p>
              <p>Spot {d.spot} · strike {d.strike}</p>
              {d.available_cash != null && (
                <p>Available cash ₹{Number(d.available_cash).toLocaleString("en-IN")}</p>
              )}
            </>
          )}
        />
      </main>
    </div>
  );
}
