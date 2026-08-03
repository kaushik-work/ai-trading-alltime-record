"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { API, TOKEN_KEY } from "../lib/format";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [imgFailed, setImgFailed] = useState(false);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API}/api/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username, password }).toString(),
      });
      if (!res.ok) {
        setError("Invalid username or password");
        setLoading(false);
        return;
      }
      const { access_token } = await res.json();
      localStorage.setItem(TOKEN_KEY, access_token);
      router.push("/");
    } catch {
      setError("Could not reach the server. Check your connection and try again.");
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-[var(--plane)] flex items-center justify-center p-4 sm:p-6">
      <div className="w-full max-w-4xl card overflow-hidden grid grid-cols-1 md:grid-cols-2">
        {/* Brand panel — hidden on phones so the form owns the viewport */}
        <div className="hidden md:block relative bg-[var(--surface-2)] min-h-[540px]">
          {!imgFailed ? (
            <img src="/parrot.png" alt="" aria-hidden="true" draggable={false}
                 className="absolute inset-0 w-full h-full object-cover select-none"
                 onError={() => setImgFailed(true)} />
          ) : (
            <div className="absolute inset-0 grid place-items-center text-[120px]" aria-hidden="true">🦜</div>
          )}
          <div className="absolute inset-0 bg-gradient-to-t from-black/45 via-black/5 to-transparent" />
          <div className="absolute bottom-0 inset-x-0 p-6">
            <p className="text-white text-sm font-semibold">The Gaint Company</p>
            <p className="text-white/75 text-xs mt-1">
              Automated price-action trading · Delta India &amp; Angel One
            </p>
          </div>
        </div>

        {/* Form */}
        <div className="flex flex-col justify-center px-6 sm:px-10 py-10 sm:py-12">
          <img src="/tgc-logo-svg.svg" alt="The Gaint Company" className="h-9 w-auto mb-8" />

          <h1 className="text-xl font-semibold text-[var(--ink)]">Welcome back</h1>
          <p className="text-sm text-[var(--ink-2)] mt-1 mb-7">Sign in to your trading dashboard.</p>

          <form onSubmit={handleLogin} className="space-y-4" noValidate>
            <div>
              <label htmlFor="username" className="label block mb-1.5">Username</label>
              <input id="username" name="username" className="field" type="text"
                     autoComplete="username" placeholder="Enter username"
                     value={username} onChange={(e) => setUsername(e.target.value)}
                     required autoFocus
                     aria-invalid={!!error} />
            </div>

            <div>
              <label htmlFor="password" className="label block mb-1.5">Password</label>
              <div className="relative">
                <input id="password" name="password" className="field pr-11"
                       type={showPass ? "text" : "password"}
                       autoComplete="current-password" placeholder="••••••••"
                       value={password} onChange={(e) => setPassword(e.target.value)}
                       required aria-invalid={!!error} />
                <button type="button" onClick={() => setShowPass((p) => !p)}
                        aria-label={showPass ? "Hide password" : "Show password"}
                        className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-[var(--r-sm)] text-[var(--ink-3)] hover:text-[var(--ink-2)]">
                  {showPass ? (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                      <line x1="1" y1="1" x2="23" y2="23" />
                    </svg>
                  ) : (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                      <circle cx="12" cy="12" r="3" />
                    </svg>
                  )}
                </button>
              </div>
            </div>

            {error && (
              <p role="alert" className="text-[13px] text-[var(--down-ink)] flex items-start gap-1.5">
                <span aria-hidden="true">✕</span>{error}
              </p>
            )}

            <button type="submit" disabled={loading || !username || !password}
                    className="btn btn-primary w-full !py-3 mt-2">
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
