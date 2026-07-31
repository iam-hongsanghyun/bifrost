/**
 * The chart the agent draws in the conversation.
 *
 * Hand-rolled SVG rather than a charting library, for two reasons that both matter
 * here. Ragnarok already owns a thousand-line ECharts layer, and importing it would
 * be exactly the coupling this whole split exists to avoid — while adding a *second*
 * copy of ECharts to Bifrost buys ~350 kB to draw lines and bars the agent picked.
 * So Bifrost renders a deliberately small vocabulary: line, area, bar, one shared x
 * axis, named series, hover readout. For anything richer the user switches to the
 * Ragnarok tab, where the real dashboard lives.
 *
 * "Interactive" here means the part that carries analytical weight: hovering reads
 * off exact values at a snapshot. Zoom and pan are deliberately absent — a chart in
 * a transcript is making one point, and if the user needs to explore they are one
 * tab away from a tool built for it.
 */
import { useMemo, useState } from 'react';

export interface ChartSpec {
  chartId?: string;
  title?: string;
  kind?: string;
  unit?: string;
  note?: string;
  x: string[];
  series: { name: string; values: number[] }[];
}

/** Colours in a fixed order, so a series keeps its colour across redraws. */
const COLOURS = ['#0f766e', '#b45309', '#1d4ed8', '#9333ea', '#be123c', '#0e7490'];

const W = 660;
const H = 240;
const PAD = { top: 12, right: 14, bottom: 30, left: 52 };

/** A tick count that lands on round-ish numbers without a full nice-number pass. */
function ticks(min: number, max: number, count = 4): number[] {
  if (min === max) return [min];
  const step = (max - min) / count;
  return Array.from({ length: count + 1 }, (_, i) => min + step * i);
}

function fmt(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e4) return `${(value / 1e3).toFixed(0)}k`;
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(abs < 1 ? 3 : 1);
}

export function Chart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const kind = spec.kind === 'bar' ? 'bar' : spec.kind === 'area' ? 'area' : 'line';

  const { lo, hi, plotW, plotH } = useMemo(() => {
    const all = spec.series.flatMap((s) => s.values);
    let min = Math.min(...all, 0);
    let max = Math.max(...all, 0);
    if (min === max) { max = min + 1; }
    return {
      lo: min, hi: max,
      plotW: W - PAD.left - PAD.right,
      plotH: H - PAD.top - PAD.bottom,
    };
  }, [spec.series]);

  const n = spec.x.length;
  const xAt = (i: number) => PAD.left + (n <= 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const yAt = (v: number) => PAD.top + plotH - ((v - lo) / (hi - lo)) * plotH;

  // Bars share each slot between series so groups do not overlap.
  const slot = n > 0 ? plotW / n : plotW;
  const barW = Math.max(1, (slot * 0.7) / Math.max(1, spec.series.length));

  // Label every nth tick so the axis never overprints itself.
  const labelEvery = Math.max(1, Math.ceil(n / 8));

  return (
    <figure className="chart">
      {spec.title && <figcaption className="chart-title">{spec.title}</figcaption>}

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="chart-svg"
        role="img"
        aria-label={spec.title || 'chart'}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * W;
          const i = Math.round(((px - PAD.left) / plotW) * (n - 1));
          setHover(i >= 0 && i < n ? i : null);
        }}
      >
        {/* y grid + labels */}
        {ticks(lo, hi).map((t, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={yAt(t)} y2={yAt(t)} className="chart-grid" />
            <text x={PAD.left - 6} y={yAt(t)} className="chart-ylabel">{fmt(t)}</text>
          </g>
        ))}

        {/* x labels */}
        {spec.x.map((label, i) =>
          i % labelEvery === 0 ? (
            <text key={i} x={xAt(i)} y={H - 10} className="chart-xlabel">
              {label.length > 10 ? `${label.slice(0, 10)}…` : label}
            </text>
          ) : null,
        )}

        {/* series */}
        {spec.series.map((s, si) => {
          const colour = COLOURS[si % COLOURS.length];
          if (kind === 'bar') {
            return (
              <g key={s.name}>
                {s.values.map((v, i) => {
                  const x = PAD.left + slot * i + slot * 0.15 + barW * si;
                  const y = yAt(Math.max(v, 0));
                  const height = Math.abs(yAt(v) - yAt(0));
                  return <rect key={i} x={x} y={y} width={barW} height={Math.max(1, height)} fill={colour} />;
                })}
              </g>
            );
          }
          const line = s.values.map((v, i) => `${i === 0 ? 'M' : 'L'}${xAt(i)},${yAt(v)}`).join(' ');
          return (
            <g key={s.name}>
              {kind === 'area' && (
                <path
                  d={`${line} L${xAt(s.values.length - 1)},${yAt(0)} L${xAt(0)},${yAt(0)} Z`}
                  fill={colour}
                  opacity={0.14}
                />
              )}
              <path d={line} fill="none" stroke={colour} strokeWidth={1.8} />
            </g>
          );
        })}

        {/* hover crosshair */}
        {hover !== null && (
          <line x1={xAt(hover)} x2={xAt(hover)} y1={PAD.top} y2={PAD.top + plotH} className="chart-cross" />
        )}
      </svg>

      <div className="chart-legend">
        {spec.series.map((s, si) => (
          <span key={s.name} className="chart-key">
            <i style={{ background: COLOURS[si % COLOURS.length] }} />
            {s.name}
            {hover !== null && s.values[hover] !== undefined && (
              <strong>{fmt(s.values[hover])}{spec.unit ? ` ${spec.unit}` : ''}</strong>
            )}
          </span>
        ))}
        {hover !== null && <span className="chart-at">at {spec.x[hover]}</span>}
      </div>

      {spec.note && <p className="chart-note">{spec.note}</p>}
    </figure>
  );
}
