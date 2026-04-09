"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Block {
  date: string;
  label: string;
  source: "config" | "runtime";
  is_today: boolean;
}

export default function EventBlocksPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [todayBlocked, setTodayBlocked] = useState(false);
  const [todayLabel, setTodayLabel] = useState<string | null>(null);
  const [newDate, setNewDate] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    const token = localStorage.getItem("aq_token");
    if (!token) { router.replace("/login"); return; }
    setAuthed(true);
    fetchBlocks(token);
  }, []);

  async function fetchBlocks(token?: string) {
    const t = token || localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/event-blocks`, {
      headers: { Authorization: `Bearer ${t}` },
    });
    if (res.ok) {
      const json = await res.json();
      setBlocks(json.blocks);
      setTodayBlocked(json.today_blocked);
      setTodayLabel(json.today_label);
    }
  }

  async function addBlock() {
    if (!newDate) { setError("Enter a date."); return; }
    setSaving(true); setError(null);
    const token = localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/event-blocks`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ date: newDate, label: newLabel || "Manual block" }),
    });
    setSaving(false);
    if (res.ok) {
      setFlash("Date blocked."); setNewDate(""); setNewLabel("");
      setTimeout(() => setFlash(null), 3000);
      fetchBlocks();
    } else {
      const j = await res.json();
      setError(j.detail || "Failed to add block.");
    }
  }

  async function removeBlock(date: string) {
    setRemoving(date);
    const token = localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/event-blocks/${date}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    setRemoving(null);
    if (res.ok) {
      setFlash("Block removed."); setTimeout(() => setFlash(null), 3000);
      fetchBlocks();
    } else {
      const j = await res.json();
      setError(j.detail || "Cannot remove.");
    }
  }

  if (!authed) return null;

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode="paper" connected={true} botStatus="unknown" onBotToggle={() => {}} />

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto p-4 md:p-6">

          <h2 className="text-lg font-bold text-gray-900 mb-1">Event Block Dates</h2>
          <p className="text-xs text-gray-500 mb-5">
            Block specific dates (Budget, RBI MPC, etc.) — bot skips all trades on these days.
            Config-hardcoded dates are read-only.
          </p>

          {/* Today's status banner */}
          <div className={`rounded-xl border p-3 mb-5 text-sm font-semibold flex items-center gap-2 ${
            todayBlocked
              ? "bg-red-50 border-red-200 text-red-700"
              : "bg-green-50 border-green-200 text-green-700"
          }`}>
            {todayBlocked ? "⛔" : "✅"}
            {todayBlocked
              ? `Today is BLOCKED — ${todayLabel}`
              : "Today is NOT blocked — bot can trade normally"}
          </div>

          {/* Flash / error */}
          {flash && <div className="mb-4 text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{flash}</div>}
          {error && <div className="mb-4 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>}

          {/* Add new block */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 mb-5">
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-3">Add Block Date</div>
            <div className="flex gap-2 flex-col sm:flex-row">
              <input
                type="date"
                value={newDate}
                onChange={e => setNewDate(e.target.value)}
                className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-400 text-gray-800"
              />
              <input
                type="text"
                value={newLabel}
                onChange={e => setNewLabel(e.target.value)}
                placeholder="Reason (e.g. RBI MPC Policy)"
                className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-400 text-gray-800 placeholder-gray-300"
              />
              <button
                onClick={addBlock}
                disabled={saving}
                className="text-sm font-bold px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              >
                {saving ? "Saving…" : "Block Date"}
              </button>
            </div>
          </div>

          {/* Block list */}
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-100">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest">All Blocked Dates</div>
            </div>
            {blocks.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-gray-400">No blocked dates configured.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-100">
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Date</th>
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Reason</th>
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Source</th>
                    <th className="px-4 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {blocks.map(b => (
                    <tr key={b.date} className={`border-b border-gray-50 ${b.is_today ? "bg-red-50" : ""}`}>
                      <td className="px-4 py-3 font-mono text-gray-800 font-semibold">
                        {b.date}
                        {b.is_today && <span className="ml-2 text-[10px] bg-red-100 text-red-600 px-1.5 py-0.5 rounded font-bold">TODAY</span>}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{b.label}</td>
                      <td className="px-4 py-3">
                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                          b.source === "config"
                            ? "bg-gray-100 text-gray-500"
                            : "bg-indigo-100 text-indigo-600"
                        }`}>
                          {b.source === "config" ? "hardcoded" : "runtime"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        {b.source === "runtime" ? (
                          <button
                            onClick={() => removeBlock(b.date)}
                            disabled={removing === b.date}
                            className="text-xs font-semibold text-red-500 hover:text-red-700 disabled:opacity-40"
                          >
                            {removing === b.date ? "…" : "Remove"}
                          </button>
                        ) : (
                          <span className="text-xs text-gray-300">config only</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <p className="mt-4 text-xs text-gray-400">
            Hardcoded dates (from config.py) cannot be removed here — redeploy to change them.
            Runtime blocks take effect immediately without redeployment.
          </p>
        </div>
      </div>
    </div>
  );
}
