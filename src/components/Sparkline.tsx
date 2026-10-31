import { useMemo } from "react";

interface Point {
  t: string;
  score: number;
}

/** Hand-rolled SVG sparkline, drawn like a pen-plotter trace on ruled paper. */
export function Sparkline({ points, height = 56 }: { points: Point[]; height?: number }) {
  const width = 360;
  const pad = 4;
  const geom = useMemo(() => {
    const pts = points.filter((p) => Number.isFinite(p.score));
    if (pts.length === 0) return null;
    const xs = (i: number) => (pts.length === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (pts.length - 1));
    const ys = (s: number) => pad + (1 - Math.max(0, Math.min(100, s)) / 100) * (height - 2 * pad);
    const coords = pts.map((p, i) => [xs(i), ys(p.score)] as const);
    const line = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${height - pad} L${coords[0][0].toFixed(1)},${height - pad} Z`;
    return { coords, line, area, last: pts[pts.length - 1] };
  }, [points, height]);

  const last = geom?.coords[geom.coords.length - 1];
  return (
    <div className="relative">
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className="block w-full"
      style={{ height }}
      role="img"
      aria-label={geom ? `Score trend, latest ${geom.last.score.toFixed(1)}` : "No trend yet"}
    >
      {/* ruled guides at 25/50/75 */}
      {[25, 50, 75].map((g) => {
        const y = pad + (1 - g / 100) * (height - 2 * pad);
        return (
          <line
            key={g}
            x1={0}
            x2={width}
            y1={y}
            y2={y}
            stroke="var(--color-rule)"
            strokeDasharray={g === 50 ? "0" : "2 3"}
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
      {geom && (
        <>
          <path d={geom.area} fill="var(--color-ink)" opacity={0.06} />
          <path
            d={geom.line}
            fill="none"
            stroke="var(--color-ink)"
            strokeWidth={1.4}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        </>
      )}
    </svg>
      {last && (
        <span
          aria-hidden
          className="absolute h-[7px] w-[7px] -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{
            left: `${(last[0] / width) * 100}%`,
            top: last[1],
            background: "var(--color-ribbon)",
            boxShadow: "0 0 0 1.5px var(--color-paper)",
          }}
        />
      )}
    </div>
  );
}
