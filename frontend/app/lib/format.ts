/** Shared formatting + session helpers.
 *
 * These were previously copy-pasted across page.tsx, controls/page.tsx and
 * NseView.tsx (three separate JWT parsers, two StatCard implementations).
 */

export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export const WS = API.replace(/^http/, "ws");
export const TOKEN_KEY = "aq_token";

/* ── Session ──────────────────────────────────────────────────────────────── */

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function isTokenValid(token: string | null): boolean {
  if (!token) return false;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 > Date.now() + 30_000; // 30s buffer
  } catch {
    return false;
  }
}

export function clearToken() {
  if (typeof window !== "undefined") localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/* ── Numbers ──────────────────────────────────────────────────────────────── */

export function usd(n: number | null | undefined, digits = 2): string {
  if (n == null || !isFinite(n)) return "—";
  const abs = Math.abs(n);
  const d = abs >= 1000 ? Math.min(digits, 0) : digits;
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  })}`;
}

export function inr(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function pct(n: number | null | undefined, digits = 2): string {
  if (n == null || !isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

/** Signed value with an explicit + / − so direction never relies on color. */
export function signed(n: number | null | undefined, digits = 2, unit = "%"): string {
  if (n == null || !isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}${unit}`;
}

export function signedUsd(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {
    maximumFractionDigits: Math.abs(n) >= 100 ? 0 : 2,
  })}`;
}

/** Direction glyph — the non-color channel for P&L sign. */
export function arrow(n: number | null | undefined): string {
  if (n == null || !isFinite(n) || n === 0) return "•";
  return n > 0 ? "▲" : "▼";
}

export function tone(n: number | null | undefined): "up" | "down" | "flat" {
  if (n == null || !isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

export function duration(minutes: number | null | undefined): string {
  if (minutes == null || !isFinite(minutes)) return "—";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
