interface Props {
  label: string;
  value: string | number;
  delta?: string;
  deltaPositive?: boolean;
  icon?: string;
}

export default function KpiCard({ label, value, delta, deltaPositive, icon }: Props) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 flex flex-col gap-1 shadow-sm">
      <span className="text-xs uppercase tracking-widest" style={{ color: "#9ca3af" }}>{icon} {label}</span>
      <span className="text-2xl font-bold" style={{ color: "#111827" }}>{value}</span>
      {delta && (
        <span className="text-xs font-medium" style={{ color: deltaPositive ? "#16a34a" : "#dc2626" }}>
          {delta}
        </span>
      )}
    </div>
  );
}
