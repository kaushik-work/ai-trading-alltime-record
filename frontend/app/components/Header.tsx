"use client";
import { useRouter } from "next/navigation";

interface Props {
  // Kept for backwards-compat with old callers; the simplified crypto-only
  // header doesn't actually need any of these except optionally the mode.
  mode?: string;
  connected?: boolean;
  botStatus?: string;
  onBotToggle?: () => void;
  errorCount?: number;
  settings?: { min_lots?: number };
}

export default function Header(_props: Props) {
  const router = useRouter();

  function logout() {
    localStorage.removeItem("aq_token");
    router.push("/login");
  }

  return (
    <header className="w-full bg-white border-b border-gray-200 px-4 md:px-6 py-2 flex items-center justify-between shadow-sm">
      {/* Left — Logo (navigates back to crypto dashboard) */}
      <div className="flex items-center gap-3 cursor-pointer" onClick={() => router.push("/crypto")}>
        <img src="/tgc-logo-svg.svg" alt="Logo" className="h-10 md:h-16 w-auto" />
      </div>

      {/* Right — Logout only (NSE trading has been retired) */}
      <div className="flex items-center gap-2 md:gap-3">
        <button
          onClick={logout}
          className="text-sm font-semibold px-3 md:px-4 py-2 border border-gray-200 text-gray-700 hover:bg-gray-50 hover:border-gray-300 rounded-lg transition-colors bg-white flex items-center gap-1.5"
          title="Sign out"
        >
          <span>🚪</span>
          <span className="hidden sm:inline">Logout</span>
        </button>
      </div>
    </header>
  );
}
