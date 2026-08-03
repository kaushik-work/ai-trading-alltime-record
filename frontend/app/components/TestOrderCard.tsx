"use client";
import { useState } from "react";
import { Banner, Card, CardHead, Pill } from "./ui";
import { API, authHeaders } from "../lib/format";

type Props = {
  title: string;
  description: string;
  endpoint: string;            // e.g. "/api/crypto/test_buy_btc"
  confirmLabel: string;
  renderSuccess?: (data: any) => React.ReactNode;
};

/** Places a REAL order to verify exchange connectivity and API permissions.
 *  Two-step confirm, and the full server error is shown rather than swallowed —
 *  the whole point of this control is to see WHY the exchange said no. */
export default function TestOrderCard({
  title, description, endpoint, confirmLabel, renderSuccess,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<any>(null);

  async function place() {
    setConfirming(false);
    setBusy(true);
    setResult(null);
    try {
      const r = await fetch(`${API}${endpoint}`, { method: "POST", headers: authHeaders() });
      let data: any = {};
      try { data = await r.json(); } catch { /* non-JSON error body */ }
      if (!r.ok) {
        throw new Error(data?.detail || data?.message || `Server returned ${r.status}`);
      }
      setResult({ ok: true, ...data });
    } catch (e: any) {
      setResult({ ok: false, error: e?.message || "Request failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="!border-[rgba(208,59,59,.3)]">
      <CardHead title={title} right={<Pill tone="down">Real money</Pill>} />
      <div className="card-pad space-y-3">
        <p className="text-[13px] text-[var(--ink-2)] leading-relaxed">{description}</p>

        {result && (
          <Banner
            tone={result.ok ? "up" : "down"}
            title={result.ok ? "Order submitted" : "Order failed"}
            body={result.ok ? undefined : result.error}
            onDismiss={() => setResult(null)}
          >
            {result.ok && renderSuccess && (
              <div className="text-xs mt-1 space-y-0.5">{renderSuccess(result)}</div>
            )}
            {result.ok && result.order_response && (
              <pre className="text-[11px] mt-2 p-2 rounded bg-black/5 overflow-x-auto max-h-40">
                {JSON.stringify(result.order_response, null, 2)}
              </pre>
            )}
          </Banner>
        )}

        {confirming ? (
          <div className="rounded-[var(--r-sm)] bg-[var(--down-wash)] border border-[rgba(208,59,59,.25)] p-3">
            <p className="text-[13px] font-semibold text-[var(--down-ink)]">{confirmLabel}</p>
            <div className="flex gap-2 mt-3">
              <button onClick={() => setConfirming(false)} className="btn btn-ghost flex-1">Cancel</button>
              <button onClick={place} disabled={busy} className="btn btn-danger flex-1">
                {busy ? "Placing…" : "Place order"}
              </button>
            </div>
          </div>
        ) : (
          <button onClick={() => setConfirming(true)} disabled={busy}
                  className="btn btn-danger-quiet w-full sm:w-auto">
            {busy ? "Placing…" : "Place test order"}
          </button>
        )}
      </div>
    </Card>
  );
}
