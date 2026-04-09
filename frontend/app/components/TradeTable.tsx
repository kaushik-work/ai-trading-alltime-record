export default function TradeTable({ trades }: { trades: any[] }) {
  if (!trades?.length) return (
    <p className="text-gray-500 text-sm py-4">No trades yet.</p>
  );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-500 text-xs uppercase border-b border-gray-100">
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
              <tr key={i} className="border-b border-gray-100 hover:bg-gray-50 transition-colors">
                <td className="py-2 font-semibold text-indigo-600">{t.symbol}</td>
                <td className={`py-2 font-semibold ${t.side === "BUY" ? "text-green-600" : "text-red-500"}`}>
                  {t.side}
                </td>
                <td className="py-2 text-right text-gray-700">₹{Number(t.price).toLocaleString()}</td>
                <td className="py-2 text-right text-gray-700">{t.quantity}</td>
                <td className={`py-2 text-right font-semibold ${pnl >= 0 ? "text-green-600" : "text-red-500"}`}>
                  {pnl !== 0 ? `₹${pnl.toFixed(2)}` : "—"}
                </td>
                <td className="py-2">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${
                    t.status === "COMPLETE" ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
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
