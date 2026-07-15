"use client";
import { useRouter } from "next/navigation";

interface Props {
  mode?: "crypto" | "nse";
  onModeChange?: (mode: "crypto" | "nse") => void;
  connected?: boolean;
  botStatus?: string;
  onBotToggle?: () => void;
  errorCount?: number;
  settings?: { min_lots?: number };
}

export default function Header({ mode = "crypto", onModeChange }: Props) {
  const router = useRouter();

  function logout() {
    localStorage.removeItem("aq_token");
    router.push("/login");
  }

  return (
    <header className="w-full bg-white border-b border-gray-200 px-4 md:px-6 py-2 flex items-center justify-between shadow-sm">
      {/* Left — Logo (navigates back to crypto dashboard) */}
      <div className="flex items-center gap-3 cursor-pointer" onClick={() => router.push("/")}>
        <img src="/tgc-logo-svg.svg" alt="Logo" className="h-10 md:h-16 w-auto" />
      </div>

      {/* Center — mode switch */}
      <div className="hidden sm:flex items-center bg-gray-100 rounded-lg p-1 border border-gray-200">
        <button
          onClick={() => onModeChange?.("crypto")}
          className={`px-4 py-1.5 text-sm font-semibold rounded-md transition-colors ${
            mode === "crypto"
              ? "bg-white text-gray-900 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          Crypto
        </button>
        <button
          onClick={() => onModeChange?.("nse")}
          className={`px-4 py-1.5 text-sm font-semibold rounded-md transition-colors ${
            mode === "nse"
              ? "bg-white text-gray-900 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          NSE
        </button>
      </div>

      {/* Right — nav + logout */}
      <div className="flex items-center gap-2 md:gap-3">
        {/* Mobile mode switch */}
        <select
          value={mode}
          onChange={(e) => onModeChange?.(e.target.value as "crypto" | "nse")}
          className="sm:hidden text-sm font-semibold px-2 py-2 border border-gray-200 rounded-lg bg-white text-gray-700"
        >
          <option value="crypto">Crypto</option>
          <option value="nse">NSE</option>
        </select>

        <button
          onClick={() => router.push("/controls")}
          className="text-sm font-semibold px-3 md:px-4 py-2 border border-gray-200 text-gray-700 hover:bg-gray-50 hover:border-gray-300 rounded-lg transition-colors bg-white flex items-center gap-1.5"
          title="Controls"
        >
          <span>🎛️</span>
          <span className="hidden sm:inline">Controls</span>
        </button>
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
