"use client";
import { useState } from "react";

/**
 * Collapsible "what is this bot doing" panel.
 *
 * Lives at the top of the dashboard. Defaults closed so the page stays
 * compact; click "What's running?" to expand.
 */
export default function StrategyExplainer() {
  const [open, setOpen] = useState(false);
  return (
    <div className="bg-white rounded-xl border border-gray-200 mb-4">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-gray-50 transition-colors rounded-xl"
      >
        <div className="flex items-center gap-3">
          <span className="text-xs font-bold uppercase tracking-widest text-gray-500">
            Strategy
          </span>
          <span className="text-sm font-bold text-gray-900">
            NIFTY Q5 Multi-Signal Shadow
          </span>
          <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-50 text-blue-700">
            FORWARD-TEST ONLY · NO REAL ORDERS
          </span>
        </div>
        <span className="text-gray-400 text-sm">
          {open ? "Hide ▲" : "What's running? ▼"}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-4 text-sm text-gray-700 border-t border-gray-100 pt-3">

          <div>
            <div className="text-xs font-bold uppercase tracking-widest text-gray-500 mb-1.5">
              The three signals
            </div>
            <div className="space-y-1.5 text-xs">
              <div className="flex gap-2">
                <span className="font-bold text-gray-900 w-44 shrink-0">q5_straddle_level</span>
                <span className="text-gray-600">
                  ATM straddle &gt; trailing-5-day 70th percentile. Rich IV with bullish drift.
                </span>
              </div>
              <div className="flex gap-2">
                <span className="font-bold text-gray-900 w-44 shrink-0">q5_straddle_mom3</span>
                <span className="text-gray-600">
                  3-bar change in ATM straddle &gt; P70. Rising IV preceding directional move.
                </span>
              </div>
              <div className="flex gap-2">
                <span className="font-bold text-gray-900 w-44 shrink-0">q5_pcr_mom3</span>
                <span className="text-gray-600">
                  3-bar change in PCR_OI &gt; P70. Short-term contrarian positioning signal.
                </span>
              </div>
            </div>
            <div className="mt-1.5 text-[10px] text-gray-400">
              Independent feature values, max pairwise |corr| under 0.5 — ensemble has real diversification.
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div>
              <div className="text-[10px] font-bold uppercase text-gray-400">Side</div>
              <div className="text-sm font-bold text-gray-900">CE only</div>
            </div>
            <div>
              <div className="text-[10px] font-bold uppercase text-gray-400">Strike</div>
              <div className="text-sm font-bold text-gray-900">ITM by 50 pts</div>
            </div>
            <div>
              <div className="text-[10px] font-bold uppercase text-gray-400">SL</div>
              <div className="text-sm font-bold text-gray-900">₹10 (fixed)</div>
            </div>
            <div>
              <div className="text-[10px] font-bold uppercase text-gray-400">RR</div>
              <div className="text-sm font-bold text-gray-900">2.25 → TP ₹22.50</div>
            </div>
          </div>

          <div>
            <div className="text-xs font-bold uppercase tracking-widest text-gray-500 mb-1.5">
              Risk controls (live)
            </div>
            <ul className="text-xs text-gray-600 space-y-0.5 ml-4 list-disc">
              <li>Max <b>4 trades / day / strategy</b> (12 total ceiling)</li>
              <li>Per-strategy loss cap <b>₹2,000</b>/day · Aggregate <b>₹3,500</b>/day</li>
              <li>Same-strike guard — refuses if another strategy holds that strike+side</li>
              <li>Lot multiplier 1× default · scales to 2× only after 30+ closed trades with PF &gt; 2</li>
              <li>Polls every <b>30 seconds</b> during market hours (09:15–15:30 IST)</li>
            </ul>
          </div>

          <div className="bg-yellow-50 border border-yellow-200 rounded-lg px-3 py-2 text-[11px] text-yellow-800">
            <b>Forward-test phase.</b> Bot logs simulated trades to Mongo
            <code className="font-mono mx-1 px-1 bg-yellow-100">shadow_trades</code>
            but places no real orders. Decision to fund Angel One and go live
            requires 4 weeks of forward data with PF &gt; 1.5.
          </div>

          <div className="text-[10px] text-gray-400">
            Full details, formulas, decision history, and Monday-onward plan in <span className="font-mono">docs/STRATEGY.pdf</span>.
          </div>
        </div>
      )}
    </div>
  );
}
