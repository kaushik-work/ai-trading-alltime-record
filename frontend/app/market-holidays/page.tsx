"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "../components/Header";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Holiday {
  date: string;
  label: string;
  source: "config" | "runtime";
  is_today: boolean;
}

export default function MarketHolidaysPage() {
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  const [holidays, setHolidays] = useState<Holiday[]>([]);
  const [todayHoliday, setTodayHoliday] = useState(false);
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
    fetchHolidays(token);
  }, []);

  async function fetchHolidays(token?: string) {
    const t = token || localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/market-holidays`, {
      headers: { Authorization: `Bearer ${t}` },
    });
    if (res.ok) {
      const json = await res.json();
      setHolidays(json.holidays);
      setTodayHoliday(json.today_holiday);
      setTodayLabel(json.today_label);
    }
  }

  function showFlash(msg: string) {
    setFlash(msg);
    setTimeout(() => setFlash(null), 3000);
  }

  async function addHoliday() {
    if (!newDate) { setError("Select a date."); return; }
    setSaving(true); setError(null);
    const token = localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/market-holidays`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ date: newDate, label: newLabel || "NSE Holiday" }),
    });
    setSaving(false);
    if (res.ok) {
      showFlash("Holiday added."); setNewDate(""); setNewLabel("");
      fetchHolidays();
    } else {
      const j = await res.json();
      setError(j.detail || "Failed to add.");
    }
  }

  async function removeHoliday(date: string) {
    setBusy(date);
    const token = localStorage.getItem("aq_token") || "";
    const res = await fetch(`${API_URL}/api/market-holidays/${date}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    setBusy(null);
    if (res.ok) { showFlash("Holiday removed."); fetchHolidays(); }
    else { const j = await res.json(); setError(j.detail || "Cannot remove hardcoded holiday."); }
  }

  if (!authed) return null;

  const upcoming = holidays.filter(h => h.date >= new Date().toISOString().slice(0, 10));
  const past     = holidays.filter(h => h.date <  new Date().toISOString().slice(0, 10));

  return (
    <div className="min-h-screen bg-[#f0f2f5] flex flex-col">
      <Header mode="paper" connected={true} botStatus="unknown" onBotToggle={() => {}} />

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto p-4 md:p-6">

          <h2 className="text-lg font-bold text-gray-900 mb-1">NSE Market Holidays</h2>
          <p className="text-xs text-gray-500 mb-5">
            Days when NSE is closed. Bot will not start, snapshot collector will skip, no orders placed.
            Hardcoded holidays cannot be removed here — edit config.py for that.
          </p>

          {/* Today holiday banner */}
          {todayHoliday && todayLabel && (
            <div className="rounded-xl border border-orange-200 bg-orange-50 p-3 mb-5 flex items-center gap-3">
              <span className="text-xl">🏖️</span>
              <div>
                <div className="text-sm font-bold text-orange-700">Today is a Market Holiday — {todayLabel}</div>
                <div className="text-xs text-orange-600 mt-0.5">Bot is paused. NSE is closed today.</div>
              </div>
            </div>
          )}

          {/* Flash / error */}
          {flash && <div className="mb-4 text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{flash}</div>}
          {error && (
            <div className="mb-4 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 cursor-pointer" onClick={() => setError(null)}>
              {error} ✕
            </div>
          )}

          {/* Add holiday */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 mb-5">
            <div className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-3">Add Holiday</div>
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
                placeholder="Holiday name (e.g. Diwali)"
                className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-400 text-gray-800 placeholder-gray-300"
              />
              <button
                onClick={addHoliday}
                disabled={saving}
                className="text-sm font-bold px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              >
                {saving ? "Saving..." : "Add"}
              </button>
            </div>
          </div>

          {/* Upcoming holidays */}
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden mb-4">
            <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
              <div className="text-xs font-bold text-gray-500 uppercase tracking-widest">Upcoming Holidays</div>
              <span className="text-xs text-gray-400">{upcoming.length} dates</span>
            </div>
            {upcoming.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-gray-400">No upcoming holidays configured.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-100">
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Date</th>
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Holiday</th>
                    <th className="px-4 py-2 text-left text-xs text-gray-500 font-semibold uppercase">Source</th>
                    <th className="px-4 py-2 text-right text-xs text-gray-500 font-semibold uppercase">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {upcoming.map(h => (
                    <tr key={h.date} className={`border-b border-gray-50 ${h.is_today ? "bg-orange-50" : ""}`}>
                      <td className="px-4 py-3 font-mono text-gray-800 font-semibold">
                        {h.date}
                        {h.is_today && (
                          <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-orange-100 text-orange-700 font-bold">TODAY</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{h.label}</td>
                      <td className="px-4 py-3">
                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                          h.source === "config"
                            ? "bg-gray-100 text-gray-500"
                            : "bg-indigo-100 text-indigo-600"
                        }`}>
                          {h.source === "config" ? "hardcoded" : "added"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        {h.source === "runtime" ? (
                          <button
                            onClick={() => removeHoliday(h.date)}
                            disabled={busy === h.date}
                            className="text-xs font-semibold text-red-500 hover:text-red-700 disabled:opacity-40"
                          >
                            {busy === h.date ? "..." : "Remove"}
                          </button>
                        ) : (
                          <span className="text-xs text-gray-300">locked</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Past holidays (collapsible feel — just smaller) */}
          {past.length > 0 && (
            <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
                <div className="text-xs font-bold text-gray-400 uppercase tracking-widest">Past Holidays</div>
                <span className="text-xs text-gray-300">{past.length} dates</span>
              </div>
              <table className="w-full text-sm">
                <tbody>
                  {past.map(h => (
                    <tr key={h.date} className="border-b border-gray-50 opacity-50">
                      <td className="px-4 py-2 font-mono text-gray-500 text-xs">{h.date}</td>
                      <td className="px-4 py-2 text-gray-400 text-xs">{h.label}</td>
                      <td className="px-4 py-2 text-xs text-gray-300">{h.source}</td>
                      <td className="px-4 py-2 text-right">
                        {h.source === "runtime" && (
                          <button
                            onClick={() => removeHoliday(h.date)}
                            disabled={busy === h.date}
                            className="text-xs text-red-400 hover:text-red-600 disabled:opacity-40"
                          >
                            {busy === h.date ? "..." : "Remove"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-4 text-xs text-gray-400">
            Hardcoded holidays (from config.py) cannot be removed via the dashboard.
            To add a new one permanently, update <code className="bg-gray-100 px-1 rounded">config.NSE_MARKET_HOLIDAYS</code> and redeploy.
          </p>
        </div>
      </div>
    </div>
  );
}
