"use client";

import type { ProgressPoint } from "@/types";

/**
 * Score-over-time line chart, drawn as inline SVG.
 *
 * Hand-rolled rather than pulling in a charting library: this is one polyline
 * over at most 50 points, and the dependency would outweigh the drawing.
 */
export function ScoreTrend({ points }: { points: ProgressPoint[] }) {
  if (points.length === 0) return null;

  const width = 640;
  const height = 160;
  const padding = { top: 12, right: 12, bottom: 12, left: 28 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  // Fixed 0-10 domain: an auto-scaled axis would make a flat run of 7s look
  // like dramatic swings.
  const x = (i: number) =>
    padding.left + (points.length === 1 ? plotWidth / 2 : (i / (points.length - 1)) * plotWidth);
  const y = (score: number) => padding.top + (1 - Math.max(0, Math.min(score, 10)) / 10) * plotHeight;

  const line = points.map((p, i) => `${x(i)},${y(p.score)}`).join(" ");
  const area = `${padding.left},${padding.top + plotHeight} ${line} ${x(points.length - 1)},${
    padding.top + plotHeight
  }`;

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-40 w-full min-w-[320px]"
        role="img"
        aria-label={`Score trend across ${points.length} interviews, most recent ${points[
          points.length - 1
        ].score.toFixed(1)} out of 10`}
      >
        {[0, 5, 10].map((tick) => (
          <g key={tick}>
            <line
              x1={padding.left}
              x2={width - padding.right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="hsl(var(--border))"
              strokeWidth={1}
            />
            <text
              x={padding.left - 8}
              y={y(tick) + 4}
              textAnchor="end"
              className="fill-muted-foreground text-[10px]"
            >
              {tick}
            </text>
          </g>
        ))}

        {points.length > 1 && (
          <polygon points={area} fill="hsl(var(--primary))" fillOpacity={0.08} />
        )}
        <polyline
          points={line}
          fill="none"
          stroke="hsl(var(--primary))"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {points.map((p, i) => (
          <circle key={p.session_id} cx={x(i)} cy={y(p.score)} r={3} fill="hsl(var(--primary))">
            <title>
              {p.title} — {p.score.toFixed(1)}/10 ·{" "}
              {new Date(p.scored_at).toLocaleDateString()}
            </title>
          </circle>
        ))}
      </svg>
    </div>
  );
}
