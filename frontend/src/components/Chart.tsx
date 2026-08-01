/**
 * The chart the agent draws in the conversation.
 *
 * This was hand-rolled SVG with a hover readout, on the reasoning that Ragnarok
 * already owns an ECharts layer and a second copy costs bundle size. That
 * reasoning does not survive contact with the actual job: a static picture with a
 * hover label is not interactive, and for a year of hourly snapshots it is
 * useless — 8760 points across 660 pixels is 13 hours per pixel, so the shape you
 * need to see is not on screen and there is no way to get to it. Bundle size is
 * also a weak argument for an app served from localhost.
 *
 * So: ECharts, pinned to the same major as Ragnarok's so both tabs behave alike.
 * What that buys, all of which matters for power-system output —
 *   - zoom and pan on the time axis (drag the slider, or scroll inside the plot)
 *   - an axis tooltip reading EVERY series at the hovered snapshot, not just one
 *   - legend toggling, to pull one generator out of a crowded dispatch stack
 *   - stacking, because the dispatch stack is the canonical chart of this domain
 *   - a second y axis, for the price series that shares an x with dispatch in MW
 *   - heatmap, for the hour-of-day × day pattern a line chart flattens away
 *
 * Two domain affordances the library does not give for free: a duration-curve
 * toggle (sort each series descending — how a modeller actually reads a load or
 * price series), and PNG export, because these end up in reports.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
// Modular imports, not `import * as echarts`: the whole library is ~1 MB and this
// chart uses four series types and five components. Type-only imports erase, so
// EChartsOption costs nothing at runtime.
import { init, use } from 'echarts/core';
import type { ECharts } from 'echarts/core';
import type { EChartsOption, SeriesOption } from 'echarts';
import { BarChart, HeatmapChart, LineChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

use([
  LineChart, BarChart, ScatterChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, VisualMapComponent,
  CanvasRenderer,
]);

export interface ChartSeries {
  name: string;
  values: number[];
  /** 'right' puts this series on the secondary axis — price against MW. */
  axis?: string;
  /** Override the series type, e.g. a price LINE over stacked dispatch areas. */
  kind?: string;
}

export interface ChartSpec {
  chartId?: string;
  title?: string;
  kind?: string;
  unit?: string;
  /** Label for the secondary axis, when any series opts into it. */
  unitRight?: string;
  note?: string;
  /** Stack the series — dispatch by carrier, capacity by technology. */
  stacked?: boolean;
  x: string[];
  series: ChartSeries[];
}

/** Fixed order, so a series keeps its colour across redraws. */
const COLOURS = ['#0f766e', '#b45309', '#1d4ed8', '#9333ea', '#be123c', '#0e7490',
                 '#4d7c0f', '#a21caf', '#0369a1', '#c2410c'];

/** Beyond this, drawing every point costs more than it shows. */
const MAX_POINTS = 4000;

function isDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

/**
 * Thin a long series without lying about it.
 *
 * min/max pair per bucket rather than every nth point: decimation by sampling
 * drops spikes, and in this domain the spike IS the answer — the peak hour, the
 * scarcity price. Extremes survive; only the count changes.
 */
function decimate(values: number[], labels: string[]): { values: number[]; labels: string[] } {
  if (values.length <= MAX_POINTS) return { values, labels };
  const bucket = Math.ceil(values.length / (MAX_POINTS / 2));
  const outV: number[] = [];
  const outL: string[] = [];
  for (let i = 0; i < values.length; i += bucket) {
    const slice = values.slice(i, i + bucket);
    let lo = 0;
    let hi = 0;
    slice.forEach((v, j) => {
      if (v < slice[lo]) lo = j;
      if (v > slice[hi]) hi = j;
    });
    const [first, second] = lo <= hi ? [lo, hi] : [hi, lo];
    outV.push(slice[first], slice[second]);
    outL.push(labels[i + first] ?? '', labels[i + second] ?? '');
  }
  return { values: outV, labels: outL };
}

function sortedDescending(values: number[]): number[] {
  return [...values].sort((a, b) => b - a);
}

/** Labels must be thinned exactly as the values were, or they drift apart. */
function axisLabels(spec: ChartSpec, duration: boolean, xLabels: string[]): string[] {
  const first = spec.series[0];
  if (!first) return xLabels;
  const values = duration ? sortedDescending(first.values) : first.values;
  return decimate(values, xLabels).labels;
}

export function Chart({ spec }: { spec: ChartSpec }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const [duration, setDuration] = useState(false);
  const [dark, setDark] = useState(isDark);

  // Follow the OS theme: these live in a transcript the user scrolls back through,
  // so a chart drawn this morning must not stay light against a dark page.
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!mq) return;
    const onChange = () => setDark(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const kind = spec.kind ?? 'line';
  const isHeatmap = kind === 'heatmap';
  const longSeries = spec.x.length > 400;

  const option = useMemo<EChartsOption>(() => {
    const text = dark ? '#e8e6e3' : '#1f2328';
    const muted = dark ? '#9aa0a6' : '#6b7280';
    const grid = dark ? '#2f3237' : '#e5e4df';
    const usesRight = spec.series.some((s) => s.axis === 'right');

    if (isHeatmap) {
      // series[i].values[j] = value for row i at column j — day × hour, usually.
      // Same wire contract as every other kind; only the reading differs.
      const data: [number, number, number][] = [];
      let min = Infinity;
      let max = -Infinity;
      spec.series.forEach((s, row) =>
        s.values.forEach((v, col) => {
          data.push([col, row, v]);
          if (v < min) min = v;
          if (v > max) max = v;
        }),
      );
      return {
        tooltip: { position: 'top' },
        // Deep enough for the axis labels AND the colour scale below them: at 62
        // the visualMap's min/max printed on top of the hour labels.
        grid: { left: 70, right: 20, top: 16, bottom: 84 },
        xAxis: { type: 'category', data: spec.x, axisLabel: { color: muted, fontSize: 10, hideOverlap: true },
                 axisLine: { lineStyle: { color: grid } } },
        yAxis: { type: 'category', data: spec.series.map((s) => s.name),
                 axisLabel: { color: muted, fontSize: 10 }, axisLine: { lineStyle: { color: grid } } },
        visualMap: {
          min: Number.isFinite(min) ? min : 0,
          max: Number.isFinite(max) ? max : 1,
          calculable: true, orient: 'horizontal', left: 'center', bottom: 2,
          itemHeight: 90, itemWidth: 11,
          textStyle: { color: muted, fontSize: 10 },
          inRange: { color: dark ? ['#1f2124', '#0f766e', '#fbbf24'] : ['#f1f5f9', '#0f766e', '#b45309'] },
        },
        series: [{
          type: 'heatmap', data, progressive: 2000,
          emphasis: { itemStyle: { borderColor: text, borderWidth: 1 } },
        }],
      };
    }

    // A duration curve is read by RANK, so the x axis stops being time.
    const xLabels = duration ? spec.x.map((_, i) => String(i + 1)) : spec.x;

    const series: SeriesOption[] = spec.series.map((s, i) => {
      const raw = duration ? sortedDescending(s.values) : s.values;
      const thinned = decimate(raw, xLabels);
      const type = s.kind ?? kind;
      const colour = COLOURS[i % COLOURS.length];
      // A right-axis series (price) must never join the stack of MW below it.
      //
      // Nor may ANY series stack in duration mode: each is sorted independently, so
      // rank 1 of solar and rank 1 of gas are different hours, and their sum is a
      // quantity that never occurred. Stacking it would draw a confident line
      // through a number that does not exist.
      const stacked = Boolean(spec.stacked) && !duration && s.axis !== 'right' && type !== 'line';
      const filled = type === 'area' || stacked;
      return {
        name: s.name,
        type: (type === 'bar' ? 'bar' : type === 'scatter' ? 'scatter' : 'line') as 'line' | 'bar' | 'scatter',
        data: thinned.values,
        yAxisIndex: s.axis === 'right' ? 1 : 0,
        stack: stacked ? 'total' : undefined,
        showSymbol: false,
        symbolSize: type === 'scatter' ? 6 : 4,
        lineStyle: { width: 1.8, color: colour },
        itemStyle: { color: colour },
        areaStyle: filled ? { color: colour, opacity: stacked ? 0.75 : 0.16 } : undefined,
        sampling: 'lttb',
      };
    });

    return {
      color: COLOURS,
      textStyle: { color: text },
      // axis trigger: every series at the hovered snapshot. That is the question
      // being asked ("what was happening at hour 47?"), not "what is this line".
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: muted } },
        backgroundColor: dark ? '#1f2124' : '#fff',
        borderColor: grid,
        textStyle: { color: text, fontSize: 12 },
      },
      legend: {
        type: 'scroll', bottom: longSeries ? 34 : 4,
        textStyle: { color: muted, fontSize: 11 },
        icon: 'roundRect', itemHeight: 8, itemWidth: 12,
      },
      grid: { left: 62, right: usesRight ? 62 : 20, top: 16, bottom: longSeries ? 76 : 46 },
      xAxis: {
        type: 'category',
        data: axisLabels(spec, duration, xLabels),
        boundaryGap: kind === 'bar',
        axisLabel: { color: muted, fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: grid } },
      },
      yAxis: [
        { type: 'value', name: spec.unit, nameTextStyle: { color: muted, fontSize: 10 },
          axisLabel: { color: muted, fontSize: 10 },
          splitLine: { lineStyle: { color: grid, type: 'dashed' } } },
        { type: 'value', name: spec.unitRight, nameTextStyle: { color: muted, fontSize: 10 },
          axisLabel: { color: muted, fontSize: 10 }, splitLine: { show: false }, show: usesRight },
      ],
      // Zoom is the whole point for a long series: drag the slider, or scroll
      // inside the plot. Both, because neither alone is discoverable.
      dataZoom: longSeries
        ? [{ type: 'inside', throttle: 50 },
           // showDetail off: the endpoint labels are drawn OUTSIDE the slider and
           // overflowed the card at both edges. The x axis and the tooltip already
           // say where you are, and the plot redraws live while dragging.
           { type: 'slider', height: 18, bottom: 6, left: 62, right: 20,
             showDetail: false, borderColor: grid,
             textStyle: { color: muted, fontSize: 9 } }]
        : [{ type: 'inside', throttle: 50 }],
      animation: false,
      series,
    };
  }, [spec, kind, isHeatmap, duration, dark, longSeries]);

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = chartRef.current ?? init(hostRef.current, undefined, { renderer: 'canvas' });
    chartRef.current = chart;
    // `true` replaces the option outright: a stale series would otherwise linger
    // when the agent redraws the same chart id with fewer series.
    chart.setOption(option, true);
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [option]);

  // Dispose on unmount only — doing it per option change would flicker.
  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  const download = () => {
    const url = chartRef.current?.getDataURL({
      pixelRatio: 2, backgroundColor: dark ? '#1f2124' : '#ffffff',
    });
    if (!url) return;
    const a = document.createElement('a');
    a.href = url;
    a.download = `${(spec.title || 'chart').replace(/[^A-Za-z0-9._-]+/g, '_')}.png`;
    a.click();
  };

  return (
    <figure className="chart">
      <div className="chart-head">
        {spec.title && <figcaption className="chart-title">{spec.title}</figcaption>}
        <div className="chart-tools">
          {!isHeatmap && (
            <button
              className={`btn-ghost${duration ? ' is-on' : ''}`}
              onClick={() => setDuration((d) => !d)}
              title="Sort every series high to low — a duration curve, read by rank instead of by time"
            >
              Duration curve
            </button>
          )}
          <button className="btn-ghost" onClick={download} title="Save this chart as a PNG">PNG</button>
        </div>
      </div>

      <div ref={hostRef} className="chart-canvas" role="img" aria-label={spec.title || 'chart'} />

      {spec.note && <p className="chart-note">{spec.note}</p>}
      {duration && (
        <p className="chart-note">
          Sorted high to low — the x axis is rank, not time. Each series is sorted on its
          own, so a given rank is a different hour in each
          {spec.stacked ? ', and stacking is switched off because that sum never occurred' : ''}.
        </p>
      )}
    </figure>
  );
}
