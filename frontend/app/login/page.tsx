"use client";
import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [error, setError]       = useState("");
  const [loading, setLoading]   = useState(false);

  const parrotRef  = useRef<HTMLDivElement>(null);
  const [imgFailed, setImgFailed] = useState(false);

  // ── Cursor tracking — parrot head follows mouse ───────────────────────────
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!parrotRef.current) return;
      const rect   = parrotRef.current.getBoundingClientRect();
      const cx     = rect.left + rect.width  / 2;
      const cy     = rect.top  + rect.height / 2;
      const dx     = (e.clientX - cx) / (window.innerWidth  / 2);
      const dy     = (e.clientY - cy) / (window.innerHeight / 2);
      const rotateY =  dx * 18;   // left-right tilt
      const rotateX = -dy * 12;   // up-down tilt
      parrotRef.current.style.transform =
        `perspective(600px) rotateY(${rotateY}deg) rotateX(${rotateX}deg)`;
    };
    window.addEventListener("mousemove", handleMouseMove);
    return () => window.removeEventListener("mousemove", handleMouseMove);
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const form = new URLSearchParams({ username, password });
      const res  = await fetch(`${API_URL}/api/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form.toString(),
      });
      if (!res.ok) { setError("Invalid username or password"); setLoading(false); return; }
      const { access_token } = await res.json();
      localStorage.setItem("aq_token", access_token);
      router.push("/");
    } catch {
      setError("Could not connect to server");
      setLoading(false);
    }
  }

  return (
    <div className="login-bg min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-4xl bg-[#13131f] border border-[#1e1e30] rounded-2xl overflow-hidden flex shadow-2xl"
           style={{ minHeight: 520 }}>

        {/* ── Left panel — parrot ── */}
        <div className="hidden md:flex w-1/2 items-end justify-center relative overflow-hidden">

          {/* full bg image */}
          {!imgFailed ? (
            <img
              src="/parrot.png"
              alt="parrot"
              className="absolute inset-0 w-full h-full object-cover select-none"
              draggable={false}
              onError={() => setImgFailed(true)}
            />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-[120px]">🦜</div>
          )}

          {/* subtle dark overlay at bottom for text legibility */}
          <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent" />

          {/* parrot head-tracking layer — invisible, covers full panel */}
          <div ref={parrotRef} className="absolute inset-0"
               style={{ transition: "transform 0.08s ease-out", willChange: "transform" }} />

          <div className="relative z-10 pb-6 text-center">
          </div>
        </div>

        {/* ── Right panel — form ── */}
        <div className="flex-1 flex flex-col justify-center px-10 py-12">

          {/* Logo */}
          <div className="mb-10">
            <img src="/tgc-logo-svg-darkbg.svg" alt="Logo" className="h-10 w-auto" />
          </div>

          <h2 className="text-2xl font-bold text-white mb-1">Welcome back</h2>
          <p className="text-sm text-gray-500 mb-8">Sign in to continue</p>

          <form onSubmit={handleLogin} className="space-y-4">

            {/* Username */}
            <div>
              <label className="text-xs text-gray-500 mb-1.5 block">Username</label>
              <input
                className="aq-input"
                type="text"
                placeholder="Enter username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                required
                autoFocus
              />
            </div>

            {/* Password */}
            <div>
              <label className="text-xs text-gray-500 mb-1.5 block">Password</label>
              <div className="relative">
                <input
                  className="aq-input pr-11"
                  type={showPass ? "text" : "password"}
                  placeholder="••••••••••••"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPass(p => !p)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
                  tabIndex={-1}
                >
                  {showPass ? (
                    // Eye-off icon
                    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                      <line x1="1" y1="1" x2="23" y2="23"/>
                    </svg>
                  ) : (
                    // Eye icon
                    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                      <circle cx="12" cy="12" r="3"/>
                    </svg>
                  )}
                </button>
              </div>
            </div>

            {error && <p className="text-red-400 text-xs">{error}</p>}

            <button type="submit" disabled={loading} className="aq-btn-primary mt-2">
              {loading ? "Signing in..." : "Log in"}
            </button>
          </form>

        </div>

      </div>
    </div>
  );
}
