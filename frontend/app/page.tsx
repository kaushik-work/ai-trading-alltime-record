"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

// The NSE dashboard has been retired. Anyone hitting / gets sent straight
// to the crypto bot, which is now the only trading surface.
export default function Home() {
  const router = useRouter();
  useEffect(() => {
    const dest = localStorage.getItem("aq_token") ? "/crypto" : "/login";
    router.replace(dest);
  }, [router]);
  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0a0a14] text-gray-400 text-sm">
      Redirecting…
    </div>
  );
}
