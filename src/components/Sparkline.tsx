import { useMemo } from "react";

interface Point {
  t: string;
  score: number;
}

/** A single quiet trend line. The y-range hugs the data so small moves stay visible. */
export function Sparkline({ points, height = 36 }: { points: Point[]; height?: number }) {
  const width = 360;
  const pad = 3;
  const geom = useMemo(() => {
    const pts = points.filter((p) => Number.isFinite(p.score));
    if (pts.length < 2) return null;
    const lo = Math.max(0, Math.min(...pts.map((p) => p.score)) - 5);
    const hi = Math.min(100, Math.max(...pts.map((p) => p.score)) + 5);
    const span = Math.max(1, hi - lo);
    const xs = (i: number) => pad + (i * (width - 2 * pad)) / (pts.length - 1);
    const ys = (s: number) => pad + (1 - (s - lo) / span) * (height - 2 * pad);
    const coords = pts.map((p, i) => [xs(i), ys(p.score)] as const);
    const line = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    return { coords, line, last: pts[pts.length - 1] };
  }, [points, height]);

  if (!geom) return null;
  const last = geom.coords[geom.coords.length - 1];
  return (
    <div className="relative" style={{ height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="block h-full w-full"
        role="img"
        aria-label={`Score trend over ${points.length} drafts, latest ${geom.last.score.toFixed(0)}`}
      >
        <path
          d={geom.line}
          fill="none"
          stroke="var(--color-text-3)"
          strokeWidth={1.25}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <span
        aria-hidden
        className="absolute h-[6px] w-[6px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent"
        style={{ left: `${(last[0] / width) * 100}%`, top: last[1] }}
      />
    </div>
  );
}
