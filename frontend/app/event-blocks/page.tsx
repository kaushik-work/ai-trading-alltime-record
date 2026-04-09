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
  unblocked: boolean;
}

export default function EventBlocksPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [todayBlocked, setTodayBlocked] = useState(false);
  const [todayUnblocked, setTodayUnblocked] = useState(false);
  const [todayLabel, setTodayLabel] = useState<string | null>(null);
  const [newDate, setNewDate] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
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
      setTodayUnblocked(json.today_unblocked ?? false);
      setTodayLabel(json.today_label);
    }
  }

  function showFlash(msg: string) {
    setFlash(msg); setTimeout(() => setFlash(null), 3000);
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
      showFlash("Date blocked."); setNewDate(""); setNewLabel("");
      fetchBlocks();
    } else {
      const j = await res.json();
      setError(j.detail || "Failed to add block.");
    }
  }

  async function removeBlock(date: string) {
    setBusy(date + ":remove");
    const token = localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/event-blocks/${date}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    setBusy(null);
    if (res.ok) { showFlash("Block removed."); fetchBlocks(); }
    else { const j = await res.json(); setError(j.detail || "Cannot remove."); }
  }

  async function unblockDate(date: string) {
    setBusy(date + ":unblock");
    const token = localStorage.getItem("aq_token") || "";
    await fetch(`${API_URL}/api/event-blocks/${date}/unblock`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    setBusy(null);
    showFlash("Trading unblocked for " + date + " — bot will trade today.");
    fetchBlocks();
  }

  async function reblock(date: string) {
    setBusy(date + ":reblock");
    const token = localStorage.getItem("aq_token") || "";
    await fetch(`${API_URL}/api/event-blocks/${date}/unblock`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    setBusy(null);
    showFlash("Block restored for " + date + ".");
    fetchBlocks();
  }

  if (!authed) return null;

  const effectivelyBlocked = todayBlocked && !todayUnblocked;

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode="paper" connected={true} botStatus="unknown" onBotToggle={() => {}} />

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto p-4 md:p-6">

          <h2 className="text-lg font-bold text-gray-900 mb-1">Event Block Dates</h2>
          <p className="text-xs text-gray-500 mb-5">
            Block or unblock specific dates — bot skips trades on blocked days.
            Unblock overrides config.py hardcoded dates instantly, no redeploy needed.
          </p>

          {/* Today's status banner */}
          {todayLabel && (
            <div className={`rounded-xl border p-3 mb-5 flex items-center justify-between ${
              effectivelyBlocked
                ? "bg-red-50 border-red-200"
                : todayUnblocked
                ? "bg-amber-50 border-amber-200"
                : "bg-green-50 border-green-200"
            }`}>
              <div className="flex items-center gap-2">
                <span className="text-lg">{effectivelyBlocked ? "⛔" : todayUnblocked ? "⚡" : "✅"}</span>
                <div>
                  <div className={`text-sm font-bold ${
                    effectivelyBlocked ? "text-red-700" : todayUnblocked ? "text-amber-700" : "text-green-700"
                  }`}>
                    {effectivelyBlocked
                      ? `Today is BLOCKED — ${todayLabel}`
                      : todayUnblocked
                      ? `Today UNBLOCKED — ${todayLabel} (override active)`
                      : "Today is not blocked"}
                  </div>
                  {todayUnblocked && (
                    <div className="text-xs text-amber-600 mt-0.5">Bot will trade normally today despite the block</div>
                  )}
                </div>
              </div>
              {/* Quick toggle for today */}
              {todayLabel && (
                todayUnblocked ? (
                  <button
                    onClick={() => reblock(blocks.find(b => b.is_today)?.date ?? "")}
                    disabled={!!busy}
                    className="text-xs font-bold px-3 py-1.5 rounded-lg border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-40 transition-colors ml-3 shrink-0"
                  >
                    Re-block
                  </button>
                ) : (
                  <button
                    onClick={() => unblockDate(blocks.find(b => b.is_today)?.date ?? "")}
                    disabled={!!busy}
                    className="text-xs font-bold px-3 py-1.5 rounded-lg border border-green-400 text-green-700 bg-green-50 hover:bg-green-100 disabled:opacity-40 transition-colors ml-3 shrink-0"
                  >
                    Unblock Today
                  </button>
                )
              )}
            </div>
          )}

          {/* Flash / error */}
          {flash && <div className="mb-4 text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{flash}</div>}
          {error && <div className="mb-4 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 cursor-pointer" onClick={() => setError(null)}>{error} ✕</div>}

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
                    <th className="px-4 py-2 text-right text-xs text-gray-500 font-semibold uppercase">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {blocks.map(b => (
                    <tr key={b.date} className={`border-b border-gray-50 ${
                      b.is_today ? (b.unblocked ? "bg-amber-50" : "bg-red-50") : ""
                    }`}>
                      <td className="px-4 py-3 font-mono text-gray-800 font-semibold">
                        {b.date}
                        {b.is_today && (
                          <span className={`ml-2 text-[10px] px-1.5 py-0.5 rounded font-bold ${
                            b.unblocked ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-600"
                          }`}>
                            {b.unblocked ? "UNBLOCKED" : "TODAY"}
                          </span>
                        )}
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
                        {b.unblocked && (
                          <span className="ml-1.5 text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-100 text-amber-700">
                            overridden
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {b.unblocked ? (
                          <button
                            onClick={() => reblock(b.date)}
                            disabled={busy === b.date + ":reblock"}
                            className="text-xs font-semibold text-red-500 hover:text-red-700 disabled:opacity-40"
                          >
                            {busy === b.date + ":reblock" ? "…" : "Re-block"}
                          </button>
                        ) : b.source === "runtime" ? (
                          <button
                            onClick={() => removeBlock(b.date)}
                            disabled={busy === b.date + ":remove"}
                            className="text-xs font-semibold text-red-500 hover:text-red-700 disabled:opacity-40"
                          >
                            {busy === b.date + ":remove" ? "…" : "Remove"}
                          </button>
                        ) : (
                          <button
                            onClick={() => unblockDate(b.date)}
                            disabled={busy === b.date + ":unblock"}
                            className="text-xs font-semibold text-green-600 hover:text-green-800 disabled:opacity-40"
                          >
                            {busy === b.date + ":unblock" ? "…" : "Unblock"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <p className="mt-4 text-xs text-gray-400">
            Unblock overrides take effect immediately — no redeploy needed. Re-block restores the original block.
          </p>
        </div>
      </div>
    </div>
  );
}
