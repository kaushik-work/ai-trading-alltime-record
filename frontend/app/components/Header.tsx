"use client";
import { useRouter } from "next/navigation";
import { clearToken } from "../lib/format";

type Mode = "crypto" | "nse";

interface Props {
  mode?: Mode;
  onModeChange?: (mode: Mode) => void;
  /** WS/poll health — rendered as a labelled pill, not color alone. */
  connection?: "live" | "connecting" | "offline";
}

const CONNECTION = {
  live:       { cls: "pill-up",      label: "Live",       dot: "var(--up)" },
  connecting: { cls: "pill-warn",    label: "Connecting", dot: "var(--warn)" },
  offline:    { cls: "pill-down",    label: "Offline",    dot: "var(--down)" },
} as const;

export default function Header({ mode = "crypto", onModeChange, connection }: Props) {
  const router = useRouter();

  function logout() {
    clearToken();
    router.push("/login");
  }

  const conn = connection ? CONNECTION[connection] : null;

  return (
    <header className="sticky top-0 z-40 bg-[var(--surface)]/95 backdrop-blur border-b border-[var(--line)]">
      <div className="max-w-[1400px] mx-auto px-3 sm:px-6">
        <div className="h-14 sm:h-16 flex items-center justify-between gap-3">
          <button onClick={() => router.push("/")} aria-label="Home"
                  className="flex items-center flex-none rounded-[var(--r-sm)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--brand)]">
            <img src="/tgc-logo-svg.svg" alt="The Gaint Company" className="h-8 sm:h-10 w-auto" />
          </button>

          {/* Desktop segmented control */}
          {onModeChange && (
            <nav className="hidden md:flex items-center p-0.5 rounded-[var(--r-sm)] bg-[var(--surface-2)] border border-[var(--line)]"
                 aria-label="Trading venue">
              {(["crypto", "nse"] as Mode[]).map((m) => (
                <button key={m} onClick={() => onModeChange(m)}
                        aria-current={mode === m ? "page" : undefined}
                        className={`px-4 py-1.5 text-[13px] font-semibold rounded-[6px] transition-colors ${
                          mode === m
                            ? "bg-[var(--surface)] text-[var(--ink)] shadow-[var(--shadow-sm)]"
                            : "text-[var(--ink-2)] hover:text-[var(--ink)]"
                        }`}>
                  {m === "crypto" ? "Crypto" : "NSE"}
                </button>
              ))}
            </nav>
          )}

          <div className="flex items-center gap-2 flex-none">
            {conn && (
              <span className={`pill ${conn.cls} hidden xs:inline-flex`}>
                <span className="dot" style={{ background: conn.dot }} aria-hidden="true" />
                {conn.label}
              </span>
            )}
            <button onClick={() => router.push("/controls")} className="btn btn-ghost !px-2.5 sm:!px-3.5" title="Controls">
              <span aria-hidden="true">⚙</span>
              <span className="hidden sm:inline">Controls</span>
            </button>
            <button onClick={logout} className="btn btn-ghost !px-2.5 sm:!px-3.5" title="Sign out">
              <span aria-hidden="true">⏻</span>
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </div>

        {/* Mobile segmented control — full width on its own row */}
        {onModeChange && (
          <nav className="md:hidden flex items-center gap-0.5 p-0.5 mb-2.5 rounded-[var(--r-sm)] bg-[var(--surface-2)] border border-[var(--line)]"
               aria-label="Trading venue">
            {(["crypto", "nse"] as Mode[]).map((m) => (
              <button key={m} onClick={() => onModeChange(m)}
                      aria-current={mode === m ? "page" : undefined}
                      className={`flex-1 py-1.5 text-[13px] font-semibold rounded-[6px] transition-colors ${
                        mode === m
                          ? "bg-[var(--surface)] text-[var(--ink)] shadow-[var(--shadow-sm)]"
                          : "text-[var(--ink-2)]"
                      }`}>
                {m === "crypto" ? "Crypto" : "NSE"}
              </button>
            ))}
          </nav>
        )}
      </div>
    </header>
  );
}
