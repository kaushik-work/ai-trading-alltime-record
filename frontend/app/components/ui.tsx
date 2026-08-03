"use client";
import { ReactNode } from "react";
import { arrow, signed, tone } from "../lib/format";

/* ── Card ─────────────────────────────────────────────────────────────────── */

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>;
}

export function CardHead({ title, right, sub }: { title: string; right?: ReactNode; sub?: string }) {
  return (
    <header className="card-head">
      <div className="min-w-0">
        <h2 className="card-title truncate">{title}</h2>
        {sub && <p className="text-[11px] text-[var(--ink-3)] mt-0.5 truncate">{sub}</p>}
      </div>
      {right && <div className="flex items-center gap-2 flex-none">{right}</div>}
    </header>
  );
}

/* ── Pill ─────────────────────────────────────────────────────────────────── */

type PillTone = "up" | "down" | "warn" | "brand" | "neutral";

export function Pill({ tone: t = "neutral", children, title }: {
  tone?: PillTone; children: ReactNode; title?: string;
}) {
  return <span className={`pill pill-${t}`} title={title}>{children}</span>;
}

/* ── Stat tile ────────────────────────────────────────────────────────────────
   One label, one value, optional footnote. `delta` is signed and always
   prefixed with a direction glyph so it never depends on hue alone.
   ──────────────────────────────────────────────────────────────────────────── */

export function Stat({ label, value, delta, deltaUnit = "%", footnote, emphasis = false, loading }: {
  label: string;
  value: ReactNode;
  delta?: number | null;
  deltaUnit?: string;
  footnote?: string;
  emphasis?: boolean;
  loading?: boolean;
}) {
  const t = tone(delta);
  const deltaColor =
    t === "up" ? "text-[var(--up-ink)]" : t === "down" ? "text-[var(--down-ink)]" : "text-[var(--ink-3)]";

  return (
    <div className="card card-pad min-w-0">
      <p className="label truncate">{label}</p>
      {loading ? (
        <div className="skel h-7 w-24 mt-2" />
      ) : (
        <p className={`mt-1.5 font-semibold text-[var(--ink)] truncate ${emphasis ? "text-2xl sm:text-3xl" : "text-lg sm:text-xl"}`}>
          {value}
        </p>
      )}
      {/* A zero delta says nothing and, when the tile's value IS the delta,
          rendering it twice just adds noise. */}
      {delta != null && delta !== 0 && !loading && (
        <p className={`mt-1 text-xs font-semibold tnum ${deltaColor}`}>
          <span aria-hidden="true">{arrow(delta)}</span> {signed(delta, 2, deltaUnit)}
        </p>
      )}
      {footnote && <p className="mt-1 text-[11px] text-[var(--ink-3)] leading-snug">{footnote}</p>}
    </div>
  );
}

/* ── Signed value — the reusable "never color alone" number ────────────────── */

export function Signed({ value, unit = "%", digits = 2, className = "" }: {
  value: number | null | undefined; unit?: string; digits?: number; className?: string;
}) {
  const t = tone(value);
  const c = t === "up" ? "text-[var(--up-ink)]" : t === "down" ? "text-[var(--down-ink)]" : "text-[var(--ink-2)]";
  return (
    <span className={`tnum font-semibold ${c} ${className}`}>
      <span aria-hidden="true">{arrow(value)}</span> {signed(value, digits, unit)}
    </span>
  );
}

/* ── Empty / error states ─────────────────────────────────────────────────── */

export function Empty({ icon = "—", title, hint }: { icon?: string; title: string; hint?: string }) {
  return (
    <div className="px-4 py-10 text-center">
      <div className="text-2xl text-[var(--ink-3)] mb-2" aria-hidden="true">{icon}</div>
      <p className="text-sm font-medium text-[var(--ink-2)]">{title}</p>
      {hint && <p className="text-xs text-[var(--ink-3)] mt-1 max-w-sm mx-auto leading-relaxed">{hint}</p>}
    </div>
  );
}

export function Banner({ tone: t, title, body, onDismiss, children }: {
  tone: "up" | "down" | "warn" | "brand";
  title: string;
  body?: string;
  onDismiss?: () => void;
  children?: ReactNode;
}) {
  const bg = { up: "var(--up-wash)", down: "var(--down-wash)", warn: "var(--warn-wash)", brand: "var(--brand-wash)" }[t];
  const fg = { up: "var(--up-ink)", down: "var(--down-ink)", warn: "var(--warn-ink)", brand: "var(--brand-ink)" }[t];
  const glyph = { up: "✓", down: "✕", warn: "!", brand: "i" }[t];
  return (
    <div className="rounded-[var(--r-md)] border p-3 sm:p-4"
         style={{ background: bg, borderColor: fg + "33" }}>
      <div className="flex items-start gap-3">
        <span aria-hidden="true"
              className="flex-none w-5 h-5 rounded-full grid place-items-center text-[11px] font-bold mt-0.5"
              style={{ background: fg, color: "#fff" }}>{glyph}</span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold" style={{ color: fg }}>{title}</p>
          {body && <p className="text-xs mt-1 leading-relaxed" style={{ color: fg }}>{body}</p>}
          {children}
        </div>
        {onDismiss && (
          <button onClick={onDismiss}
                  className="flex-none text-xs font-medium underline underline-offset-2 opacity-70 hover:opacity-100"
                  style={{ color: fg }}>
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}

/* ── Toggle switch ────────────────────────────────────────────────────────── */

export function Toggle({ checked, onChange, disabled, ariaLabel, size = "md" }: {
  checked: boolean; onChange: (v: boolean) => void;
  disabled?: boolean; ariaLabel: string; size?: "sm" | "md";
}) {
  // Explicit pixel values, not scale steps: `h-4.5` is not in Tailwind's
  // default scale, so it silently produced a zero-size knob and the switch
  // rendered as a solid green pill.
  const sm = size === "sm";
  const box = sm ? "h-5 w-9" : "h-6 w-11";
  const knob = sm ? "h-[14px] w-[14px]" : "h-4 w-4";
  const shift = checked
    ? (sm ? "translate-x-[19px]" : "translate-x-[24px]")
    : (sm ? "translate-x-[3px]" : "translate-x-[4px]");
  return (
    <button
      type="button" role="switch" aria-checked={checked} aria-label={ariaLabel}
      disabled={disabled} onClick={() => onChange(!checked)}
      className={`relative inline-flex ${box} flex-none items-center rounded-full transition-colors
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand)]
        ${disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}
      style={{ background: checked ? "var(--up)" : "#cbc9c2" }}
    >
      <span className={`inline-block ${knob} transform rounded-full bg-white shadow-sm transition-transform ${shift}`} />
    </button>
  );
}
