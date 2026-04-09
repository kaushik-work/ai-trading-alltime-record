"use client";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function ZerodhaCallbackInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<"processing" | "success" | "error">("processing");
  const [message, setMessage] = useState("Exchanging token with Zerodha…");

  useEffect(() => {
    const requestToken = params.get("request_token");
    const action       = params.get("action");
    const loginStatus  = params.get("status");

    if (!requestToken || action !== "login" || loginStatus !== "success") {
      setStatus("error");
      setMessage(
        loginStatus === "error"
          ? "Zerodha login was cancelled or failed."
          : "Invalid callback — missing request_token."
      );
      return;
    }

    const token = localStorage.getItem("aq_token");
    if (!token) {
      router.replace("/login");
      return;
    }

    fetch(`${API_URL}/api/zerodha/callback`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ request_token: requestToken }),
    })
      .then(async res => {
        if (res.ok) {
          setStatus("success");
          setMessage("Zerodha token updated successfully. Bot will use the new token immediately.");
          setTimeout(() => router.push("/"), 2500);
        } else {
          const j = await res.json().catch(() => ({}));
          setStatus("error");
          setMessage(j.detail || "Failed to exchange token.");
        }
      })
      .catch(err => {
        setStatus("error");
        setMessage(`Network error: ${err.message}`);
      });
  }, []);

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm p-8 max-w-md w-full text-center">

        {status === "processing" && (
          <>
            <div className="w-12 h-12 mx-auto mb-4 rounded-full border-4 border-indigo-200 border-t-indigo-600 animate-spin" />
            <h2 className="text-lg font-bold text-gray-900 mb-2">Connecting to Zerodha</h2>
            <p className="text-sm text-gray-500">{message}</p>
          </>
        )}

        {status === "success" && (
          <>
            <div className="w-12 h-12 mx-auto mb-4 rounded-full bg-green-100 flex items-center justify-center text-2xl">✓</div>
            <h2 className="text-lg font-bold text-gray-900 mb-2">Token Updated</h2>
            <p className="text-sm text-gray-500">{message}</p>
            <p className="text-xs text-gray-400 mt-3">Redirecting to dashboard…</p>
          </>
        )}

        {status === "error" && (
          <>
            <div className="w-12 h-12 mx-auto mb-4 rounded-full bg-red-100 flex items-center justify-center text-2xl">✗</div>
            <h2 className="text-lg font-bold text-gray-900 mb-2">Token Exchange Failed</h2>
            <p className="text-sm text-red-600 mb-4">{message}</p>
            <button
              onClick={() => router.push("/")}
              className="text-sm font-semibold px-5 py-2 bg-gray-900 text-white rounded-lg hover:bg-gray-700 transition-colors"
            >
              Back to Dashboard
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default function ZerodhaCallbackPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-[#f0f2f5] flex items-center justify-center">
        <div className="w-10 h-10 rounded-full border-4 border-indigo-200 border-t-indigo-600 animate-spin" />
      </div>
    }>
      <ZerodhaCallbackInner />
    </Suspense>
  );
}
