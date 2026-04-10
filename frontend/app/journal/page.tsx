"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useWebSocket } from "../hooks/useWebSocket";
import Header from "../components/Header";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_URL  = API_URL.replace(/^http/, "ws") + "/ws";

function authHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${localStorage.getItem("aq_token") ?? ""}`,
  };
}

export default function JournalPage() {
  const router = useRouter();
  const { data: wsData, connected } = useWebSocket(WS_URL);

  const [dates,         setDates]         = useState<string[]>([]);
  const [selectedDate,  setSelectedDate]  = useState<string>("");
  const [journal,       setJournal]       = useState<any>(null);
  const [loading,       setLoading]       = useState(false);
  const [notes,         setNotes]         = useState("");
  const [notesSaved,    setNotesSaved]    = useState(false);
  const [savingNotes,   setSavingNotes]   = useState(false);

  useEffect(() => {
    if (!localStorage.getItem("aq_token")) { router.replace("/login"); return; }
    fetchDates();
  }, []);

  async function fetchDates() {
    try {
      const res = await fetch(`${API_URL}/api/journals`, { headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      setDates(data.dates ?? []);
      if (data.dates?.length > 0) loadJournal(data.dates[0]);
    } catch {}
  }

  async function loadJournal(date: string) {
    setSelectedDate(date);
    setJournal(null);
    setNotesSaved(false);
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/journals/${date}`, { headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      setJournal(data);
      setNotes(data.learning_notes ?? "");
    } catch {} finally {
      setLoading(false);
    }
  }

  async function saveNotes() {
    setSavingNotes(true);
    setNotesSaved(false);
    try {
      const res = await fetch(`${API_URL}/api/journals/${selectedDate}/notes`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ notes }),
      });
      if (res.ok) { setNotesSaved(true); setTimeout(() => setNotesSaved(false), 3000); }
    } catch {} finally {
      setSavingNotes(false);
    }
  }

  async function saveNow() {
    try {
      await fetch(`${API_URL}/api/journals/save-now`, { method: "POST", headers: authHeaders() });
      await fetchDates();
    } catch {}
  }

  const summary = journal?.summary ?? {};
  const breakdown = journal?.strategy_breakdown ?? {};
  const trades = journal?.trades ?? [];

  return (
    <div className="min-h-screen bg-[#f0f2f5]">
      <Header mode={wsData?.mode ?? "paper"} connected={connected}
              botStatus={wsData?.bot_status ?? "unknown"} onBotToggle={() => {}} />

      <div className="max-w-7xl mx-auto p-6 space-y-5">

        {/* Nav */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={() => router.push("/")}
              className="text-gray-400 hover:text-gray-700 text-sm font-medium">← Back</button>
            <div className="w-px h-4 bg-gray-200" />
            <span className="font-bold text-gray-800 text-sm">Trading Journal</span>
          </div>
          <button onClick={saveNow}
            className="text-xs px-3 py-1.5 border border-gray-300 rounded-lg text-gray-600 hover:border-gray-400 font-medium">
            💾 Save Today Now
          </button>
        </div>

        <div className="flex gap-5">

          {/* ── Left: date list ── */}
          <div className="w-48 flex-shrink-0">
            <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100">
                <span className="text-xs font-bold text-gray-500 uppercase tracking-widest">Past Days</span>
              </div>
              {dates.length === 0 ? (
                <div className="px-4 py-6 text-xs text-gray-400 text-center">
                  No journals yet.<br />First saves at 15:20.
                </div>
              ) : (
                <div className="divide-y divide-gray-50">
                  {dates.map(d => (
                    <button key={d} onClick={() => loadJournal(d)}
                      className={`w-full text-left px-4 py-3 text-sm transition-colors ${
                        d === selectedDate
                          ? "bg-indigo-50 text-indigo-700 font-semibold"
                          : "text-gray-600 hover:bg-gray-50"
                      }`}>
                      {d}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* ── Right: journal detail ── */}
          <div className="flex-1 space-y-4">

            {loading && (
              <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
                Loading...
              </div>
            )}

            {!loading && !journal && dates.length > 0 && (
              <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400 text-sm">
                Select a date
              </div>
            )}

            {journal && (
              <>
                {/* Summary KPIs */}
                <div className="grid grid-cols-5 gap-3">
                  {[
                    { label: "Total P&L", value: `₹${summary.total_pnl?.toLocaleString("en-IN")}`,
                      color: summary.total_pnl >= 0 ? "#16a34a" : "#dc2626" },
                    { label: "Trades", value: summary.completed_trades ?? 0 },
                    { label: "Wins", value: summary.wins ?? 0, color: "#16a34a" },
                    { label: "Losses", value: summary.losses ?? 0, color: "#dc2626" },
                    { label: "Win Rate", value: `${summary.win_rate ?? 0}%` },
                  ].map(k => (
                    <div key={k.label} className="bg-white rounded-xl border border-gray-200 p-4">
                      <div className="text-[10px] uppercase tracking-widest font-semibold text-gray-400 mb-1">{k.label}</div>
                      <div className="text-xl font-bold" style={{ color: k.color ?? "#111827" }}>{k.value}</div>
                    </div>
                  ))}
                </div>

                {/* Strategy breakdown */}
                <div className="grid grid-cols-3 gap-3">
                  {Object.entries(breakdown).map(([name, s]: any) => (
                    <div key={name} className="bg-white rounded-xl border border-gray-200 p-4">
                      <div className="text-sm font-bold text-gray-700 mb-2">{name}</div>
                      <div className={`text-lg font-bold ${s.pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                        {s.pnl >= 0 ? "+" : ""}₹{s.pnl?.toLocaleString("en-IN")}
                      </div>
                      <div className="flex gap-3 mt-1 text-xs text-gray-400">
                        <span>{s.trades} trades</span>
                        <span className="text-green-600">{s.wins}W</span>
                        <span className="text-red-500">{s.losses}L</span>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Trade table */}
                {trades.length > 0 && (
                  <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
                    <div className="px-5 py-3 border-b border-gray-100">
                      <span className="text-sm font-bold text-gray-800">Trades — {selectedDate}</span>
                    </div>
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="bg-gray-50 border-b border-gray-100">
                          {["Strategy","Option","Strike","Entry ₹","Lots","P&L","Close","Score","Entry Time","Exit Time"].map(h => (
                            <th key={h} className="px-3 py-2 text-left text-gray-500 font-semibold uppercase">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {trades.map((t: any, i: number) => {
                          const pnl = t.pnl ?? 0;
                          const optColor = t.option_type === "CE"
                            ? "bg-blue-100 text-blue-700" : "bg-orange-100 text-orange-700";
                          return (
                            <>
                              <tr key={`${i}-r`} className="border-b border-gray-50 hover:bg-gray-50">
                                <td className="px-3 py-2 font-semibold text-gray-700">{t.strategy}</td>
                                <td className="px-3 py-2">
                                  <span className={`px-1.5 py-0.5 rounded font-bold ${optColor}`}>{t.option_type}</span>
                                </td>
                                <td className="px-3 py-2 text-gray-700">{t.strike ?? "—"}</td>
                                <td className="px-3 py-2 text-gray-700">
                                  {t.entry_price ? `₹${Number(t.entry_price).toLocaleString("en-IN")}` : "—"}
                                </td>
                                <td className="px-3 py-2 text-gray-500">{t.lot_size ?? 65}</td>
                                <td className={`px-3 py-2 font-bold ${pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                                  {pnl >= 0 ? "+" : ""}₹{pnl.toLocaleString("en-IN")}
                                </td>
                                <td className="px-3 py-2">
                                  <span className={`px-1.5 py-0.5 rounded font-medium ${
                                    t.close_reason === "TP" ? "bg-green-100 text-green-700" :
                                    t.close_reason === "SL" ? "bg-red-100 text-red-700" :
                                    "bg-gray-100 text-gray-500"
                                  }`}>{t.close_reason ?? "—"}</span>
                                </td>
                                <td className="px-3 py-2 text-gray-500">{t.score ? Number(t.score).toFixed(1) : "—"}</td>
                                <td className="px-3 py-2 text-gray-400">
                                  {t.entry_time ? new Date(t.entry_time).toLocaleTimeString("en-IN") : "—"}
                                </td>
                                <td className="px-3 py-2 text-gray-400">
                                  {t.exit_time ? new Date(t.exit_time).toLocaleTimeString("en-IN") : "—"}
                                </td>
                              </tr>
                              {(t.entry_remark || t.exit_remark) && (
                                <tr key={`${i}-rem`} className="border-b border-gray-100 bg-gray-50/50">
                                  <td colSpan={10} className="px-4 py-2 space-y-1">
                                    {t.entry_remark && (
                                      <div className="flex gap-2 text-xs">
                                        <span className="font-bold text-indigo-500 shrink-0">📝 Entry:</span>
                                        <span className="text-gray-600">{t.entry_remark}</span>
                                      </div>
                                    )}
                                    {t.exit_remark && (
                                      <div className="flex gap-2 text-xs">
                                        <span className="font-bold text-amber-500 shrink-0">🔍 Review:</span>
                                        <span className="text-gray-600">{t.exit_remark}</span>
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              )}
                            </>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {trades.length === 0 && (
                  <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400 text-sm">
                    No completed trades on this day.
                  </div>
                )}

                {/* Learning notes */}
                <div className="bg-white rounded-xl border border-gray-200 p-5">
                  <div className="flex items-center justify-between mb-3">
                    <div>
                      <div className="text-sm font-bold text-gray-800">Learning Notes</div>
                      <div className="text-xs text-gray-400 mt-0.5">
                        What did you learn? What to improve? What worked?
                      </div>
                    </div>
                    <button onClick={saveNotes} disabled={savingNotes}
                      className="text-sm px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-semibold rounded-lg transition-colors">
                      {savingNotes ? "Saving..." : notesSaved ? "✓ Saved" : "Save Notes"}
                    </button>
                  </div>
                  <textarea
                    value={notes}
                    onChange={e => { setNotes(e.target.value); setNotesSaved(false); }}
                    rows={6}
                    placeholder={`e.g.\n- Musashi nailed the trend today — the HA flip confirmation really helps\n- Raijin got stopped out twice near VWAP — need tighter entry filter\n- Would trailing the SL after +1R have made more? Yes on trade 1`}
                    className="w-full text-sm border border-gray-200 rounded-lg px-4 py-3 outline-none focus:border-indigo-400 resize-y text-gray-700 placeholder-gray-300"
                  />
                </div>
              </>
            )}

          </div>
        </div>
      </div>
    </div>
  );
}
