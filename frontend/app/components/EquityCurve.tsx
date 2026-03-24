"use client";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

export default function EquityCurve({ data }: { data: any[] }) {
  if (!data?.length) return (
    <div className="flex items-center justify-center h-48 text-gray-600 text-sm">
      No closed trades yet
    </div>
  );

  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={data}>
        <XAxis dataKey="timestamp" hide />
        <YAxis tickFormatter={(v) => `₹${v}`} tick={{ fill: "#6b7280", fontSize: 11 }} width={70} />
        <Tooltip
          contentStyle={{ background: "#13131f", border: "1px solid #1e1e30", borderRadius: 8 }}
          labelStyle={{ color: "#6b7280" }}
          formatter={(v: any) => [`₹${Number(v).toFixed(2)}`, "P&L"]}
        />
        <ReferenceLine y={0} stroke="#1e1e30" />
        <Line
          type="monotone" dataKey="pnl"
          stroke="#00d4ff" strokeWidth={2}
          dot={false} activeDot={{ r: 4 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
