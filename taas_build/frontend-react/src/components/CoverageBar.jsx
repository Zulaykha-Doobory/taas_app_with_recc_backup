// CoverageBar.jsx — requirements coverage % bar shown at the top of results.
export default function CoverageBar({ coverage, covered = [], gaps = [], source }) {
  if (coverage == null) return null;
  const pct = Math.round(coverage);
  const color = pct >= 80 ? "#22c55e" : pct >= 50 ? "#eab308" : "#ef4444";

  return (
    <div className="mb-5 rounded-xl bg-[#1c1c26] border border-[#2f2f3d] p-4">
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-sm font-medium text-[#cfcfda]">
          Requirements coverage
        </span>
        <span className="text-sm font-medium" style={{ color }}>
          {pct}%
        </span>
      </div>
      <div className="h-3.5 rounded-full bg-neutral-950 border border-[#2f2f3d] overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-700"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <p className="text-xs text-[#cfcfda] mt-1.5">
        {covered.length} covered · {gaps.length} gap{gaps.length === 1 ? "" : "s"}
        {source ? ` from ${source}` : ""}
      </p>
    </div>
  );
}
