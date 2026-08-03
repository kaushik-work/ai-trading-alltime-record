"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";
import { Banner, Card, CardHead, Empty, Pill, Toggle } from "../components/ui";
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

  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);
  const [testConfirm, setTestConfirm] = useState(false);

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

  async function placeTestOrder() {
    setTestConfirm(false);
    setTestBusy(true);
    setTestResult(null);
    try {
      const r = await fetch(`${API}/api/crypto/test_buy_btc`, { method: "POST", headers: authHeaders() });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `Server returned ${r.status}`);
      setTestResult({ ok: true, ...data });
    } catch (e: any) {
      setTestResult({ ok: false, error: e?.message || "Test order failed" });
    } finally {
      setTestBusy(false);
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

        {/* ── Danger zone ──────────────────────────────────────────────────
            This places a REAL leveraged order. It used to sit on the main
            dashboard beside the kill switch, one misclick apart. ────────── */}
        <Card className="!border-[rgba(208,59,59,.3)]">
          <CardHead title="Danger zone" right={<Pill tone="down">Real money</Pill>} />
          <div className="card-pad space-y-3">
            <div>
              <p className="text-sm font-semibold text-[var(--ink)]">Place a test BTC order</p>
              <p className="text-[13px] text-[var(--ink-2)] mt-1 leading-relaxed">
                Submits a live market buy for 1 BTCUSD contract at 200× leverage to
                verify exchange connectivity and API permissions. This spends real
                funds and opens a real position you must close yourself.
              </p>
            </div>

            {testResult && (
              <Banner tone={testResult.ok ? "up" : "down"}
                      title={testResult.ok ? "Order submitted" : "Order failed"}
                      body={testResult.ok
                        ? `${testResult.symbol} ${String(testResult.side || "").toUpperCase()} · size ${testResult.size} · ${testResult.leverage}× · mark ${usd(testResult.mark_price)}`
                        : testResult.error}
                      onDismiss={() => setTestResult(null)} />
            )}

            {testConfirm ? (
              <div className="rounded-[var(--r-sm)] bg-[var(--down-wash)] border border-[rgba(208,59,59,.25)] p-3">
                <p className="text-[13px] font-semibold text-[var(--down-ink)]">
                  Confirm: place a real 200× leveraged buy order?
                </p>
                <div className="flex gap-2 mt-3">
                  <button onClick={() => setTestConfirm(false)} className="btn btn-ghost flex-1">Cancel</button>
                  <button onClick={placeTestOrder} disabled={testBusy} className="btn btn-danger flex-1">
                    {testBusy ? "Placing…" : "Place order"}
                  </button>
                </div>
              </div>
            ) : (
              <button onClick={() => setTestConfirm(true)} disabled={testBusy}
                      className="btn btn-danger-quiet w-full sm:w-auto">
                {testBusy ? "Placing…" : "Place test order"}
              </button>
            )}
          </div>
        </Card>
      </main>
    </div>
  );
}
