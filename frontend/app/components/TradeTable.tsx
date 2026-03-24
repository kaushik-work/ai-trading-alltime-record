export default function TradeTable({ trades }: { trades: any[] }) {
  if (!trades?.length) return (
    <p className="text-gray-600 text-sm py-4">No trades yet.</p>
  );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-500 text-xs uppercase border-b border-[#1e1e30]">
            <th className="py-2 text-left">Symbol</th>
            <th className="py-2 text-left">Side</th>
            <th className="py-2 text-right">Price</th>
            <th className="py-2 text-right">Qty</th>
            <th className="py-2 text-right">P&L</th>
            <th className="py-2 text-left">Status</th>
            <th className="py-2 text-left">Time</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const pnl = t.pnl ?? 0;
            return (
              <tr key={i} className="border-b border-[#1e1e30] hover:bg-[#1a1a2e] transition-colors">
                <td className="py-2 font-medium text-[#00d4ff]">{t.symbol}</td>
                <td className={`py-2 font-semibold ${t.side === "BUY" ? "text-green-400" : "text-red-400"}`}>
                  {t.side}
                </td>
                <td className="py-2 text-right">₹{Number(t.price).toLocaleString()}</td>
                <td className="py-2 text-right">{t.quantity}</td>
                <td className={`py-2 text-right font-semibold ${pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {pnl !== 0 ? `₹${pnl.toFixed(2)}` : "—"}
                </td>
                <td className="py-2">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${
                    t.status === "COMPLETE" ? "bg-green-900 text-green-300" : "bg-gray-800 text-gray-400"
                  }`}>{t.status}</span>
                </td>
                <td className="py-2 text-gray-500 text-xs">
                  {t.timestamp ? new Date(t.timestamp).toLocaleTimeString("en-IN") : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
