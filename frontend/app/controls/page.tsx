"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const _API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Instrument = {
  name: string;
  enabled: boolean;
};

type Strategy = {
  name: string;
  enabled: boolean;
  instruments: Instrument[];
};

type ToggleState = {
  loading: boolean;
  error: string | null;
  strategies: Strategy[];
};

function isTokenValid(token: string | null): boolean {
  if (!token) return false;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 > Date.now() + 30_000;
  } catch {
    return false;
  }
}

function logoutAndLogin(router: ReturnType<typeof useRouter>) {
  localStorage.removeItem("aq_token");
  router.replace("/login");
}

function strategyDisplayName(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function instrumentDisplayName(name: string): string {
  return name.replace("USD", "").toUpperCase();
}

export default function StrategiesPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [state, setState] = useState<ToggleState>({
    loading: true,
    error: null,
    strategies: [],
  });
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  async function fetchStrategies() {
    const t = localStorage.getItem("aq_token");
    if (!isTokenValid(t)) {
      logoutAndLogin(router);
      return;
    }
    try {
      const r = await fetch(`${_API}/api/crypto/strategies`, {
        headers: { Authorization: `Bearer ${t}` },
      });
      if (r.status === 401) {
        logoutAndLogin(router);
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setState({ loading: false, error: null, strategies: data.strategies ?? [] });
    } catch (e: any) {
      setState((s) => ({ ...s, loading: false, error: e?.message || "Failed to load strategies" }));
    }
  }

  async function toggleStrategy(name: string, enabled: boolean) {
    const t = localStorage.getItem("aq_token");
    if (!isTokenValid(t)) {
      logoutAndLogin(router);
      return;
    }
    const key = `strategy:${name}`;
    setBusy((b) => ({ ...b, [key]: true }));
    try {
      const r = await fetch(`${_API}/api/crypto/strategies/${name}/${enabled ? "enable" : "disable"}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${t}` },
      });
      if (r.status === 401) {
        logoutAndLogin(router);
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await fetchStrategies();
    } catch (e: any) {
      setState((s) => ({ ...s, error: e?.message || "Toggle failed" }));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }

  async function toggleInstrument(strategyName: string, instrumentName: string, enabled: boolean) {
    const t = localStorage.getItem("aq_token");
    if (!isTokenValid(t)) {
      logoutAndLogin(router);
      return;
    }
    const key = `instrument:${strategyName}:${instrumentName}`;
    setBusy((b) => ({ ...b, [key]: true }));
    try {
      const r = await fetch(
        `${_API}/api/crypto/strategies/${strategyName}/instruments/${instrumentName}/${enabled ? "enable" : "disable"}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${t}` },
        }
      );
      if (r.status === 401) {
        logoutAndLogin(router);
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await fetchStrategies();
    } catch (e: any) {
      setState((s) => ({ ...s, error: e?.message || "Toggle failed" }));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }

  useEffect(() => {
    const t = localStorage.getItem("aq_token");
    if (!isTokenValid(t)) {
      logoutAndLogin(router);
    } else {
      setAuthed(true);
      fetchStrategies();
    }
  }, []);

  if (!authed) return null;

  return (
    <div className="min-h-screen bg-[#0a0a14] text-gray-200">
      <Header mode="crypto" connected={true} botStatus="running" settings={{ min_lots: 1 }} />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <div className="mb-6">
          <h1 className="text-2xl sm:text-3xl font-bold text-[#627eea]">Controls</h1>
          <p className="text-xs text-gray-500 mt-1">
            Enable or disable strategies and their instruments. Disabled strategies stop taking new entries; open positions are still managed until they close.
          </p>
        </div>

        {state.error && (
          <div className="border border-red-700 bg-red-950/30 rounded-lg p-4 mb-6">
            <p className="text-sm text-red-300">{state.error}</p>
            <button
              onClick={() => setState((s) => ({ ...s, error: null }))}
              className="text-xs text-gray-500 hover:text-white mt-2"
            >
              dismiss
            </button>
          </div>
        )}

        {state.loading ? (
          <p className="text-sm text-gray-500">Loading strategy state…</p>
        ) : state.strategies.length === 0 ? (
          <p className="text-sm text-gray-500">No strategies configured.</p>
        ) : (
          <div className="space-y-4">
            {state.strategies.map((strategy) => {
              const strategyKey = `strategy:${strategy.name}`;
              const strategyBusy = busy[strategyKey];
              return (
                <div
                  key={strategy.name}
                  className="border border-[#1e1e30] rounded-2xl p-4 sm:p-5 bg-[#0e0e1a]"
                >
                  <div className="flex items-center justify-between mb-4">
                    <div>
                      <h2 className="text-base sm:text-lg font-semibold text-white">
                        {strategyDisplayName(strategy.name)}
                      </h2>
                      <p className="text-xs text-gray-500">
                        {strategy.enabled ? (
                          <span className="text-green-400">● Enabled</span>
                        ) : (
                          <span className="text-red-400">● Disabled</span>
                        )}
                      </p>
                    </div>
                    <Toggle
                      checked={strategy.enabled}
                      onChange={(v) => toggleStrategy(strategy.name, v)}
                      disabled={strategyBusy}
                      ariaLabel={`Toggle ${strategy.name}`}
                    />
                  </div>

                  {strategy.instruments.length > 0 && (
                    <div className="border-t border-[#1e1e30] pt-4">
                      <p className="text-xs text-gray-500 mb-3">Instruments</p>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        {strategy.instruments.map((inst) => {
                          const instKey = `instrument:${strategy.name}:${inst.name}`;
                          const instBusy = busy[instKey];
                          const disabledByStrategy = !strategy.enabled;
                          return (
                            <div
                              key={inst.name}
                              className={`flex items-center justify-between rounded-lg px-3 py-2 border ${
                                disabledByStrategy
                                  ? "border-[#1e1e30] bg-[#0a0a14] opacity-60"
                                  : "border-[#1e1e30] bg-[#13131f]"
                              }`}
                            >
                              <div>
                                <p className="text-sm font-medium text-gray-200">
                                  {instrumentDisplayName(inst.name)}
                                </p>
                                <p className="text-[10px] text-gray-500">
                                  {inst.enabled && strategy.enabled
                                    ? "Active"
                                    : disabledByStrategy
                                    ? "Strategy disabled"
                                    : "Paused"}
                                </p>
                              </div>
                              <Toggle
                                checked={inst.enabled && strategy.enabled}
                                onChange={(v) => toggleInstrument(strategy.name, inst.name, v)}
                                disabled={instBusy || disabledByStrategy}
                                ariaLabel={`Toggle ${inst.name}`}
                                size="sm"
                              />
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  disabled,
  ariaLabel,
  size = "md",
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
  size?: "sm" | "md";
}) {
  const h = size === "sm" ? "h-5 w-9" : "h-6 w-11";
  const dot = size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4";
  const translate = checked ? (size === "sm" ? "translate-x-4" : "translate-x-5") : "translate-x-1";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex ${h} items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-[#627eea] focus:ring-offset-2 focus:ring-offset-[#0a0a14] ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      } ${checked ? "bg-green-500" : "bg-gray-600"}`}
    >
      <span
        className={`inline-block ${dot} transform rounded-full bg-white transition-transform ${translate}`}
      />
    </button>
  );
}
