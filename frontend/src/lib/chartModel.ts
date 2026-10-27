/**
 * The chart's logic, with no React and no DOM: spec in, ECharts option out.
 *
 * Kept apart from the component so the parts that were wrong are testable — the
 * duration curve, the log scale, what a stack means — and so one definition serves
 * the transcript, the State panel's dashboard and the full-screen view.
 */
import type { EChartsOption, SeriesOption } from 'echarts';
import { csvCell, escapeHtml, fmtCompact, fmtNumber } from './format';

export interface ChartSeries {
  name: string;
  /** `null` is a gap — kept in place so the series stays aligned with `x`. */
  values: (number | null)[];
  /** 'right' puts this series on the secondary axis — price against MW. */
  axis?: string;
  /** Override the series type, e.g. a price LINE over stacked dispatch areas. */
  kind?: string;
  color?: string;
}

export interface ChartSpec {
  chartId?: string;
  title?: string;
  kind?: string;
  unit?: string;
  unitRight?: string;
  note?: string;
  stacked?: boolean;
  x: string[];
  series: ChartSeries[];
  /** The agent sent this many points; the server thinned to `x.length`. */
  thinnedFrom?: number;
}

export type ChartType = 'line' | 'area' | 'stacked-area' | 'bar' | 'stacked-bar' | 'scatter';

export const CHART_TYPES: { value: ChartType; label: string }[] = [
  { value: 'line', label: 'Line' },
  { value: 'area', label: 'Area' },
  { value: 'stacked-area', label: 'Stacked area' },
  { value: 'bar', label: 'Bar' },
  { value: 'stacked-bar', label: 'Stacked bar' },
];

export interface ChartView {
  type: ChartType;
  /** Sort every series high to low; the x axis becomes rank. */
  duration: boolean;
  log: boolean;
}

export interface ChartTheme {
  text: string;
  muted: string;
  border: string;
  surface: string;
  brand: string;
  warn: string;
  dark: boolean;
}

export interface ZoomWindow {
  start: number;
  end: number;
}

export interface ChartParams {
  spec: ChartSpec;
  view: ChartView;
  hidden: ReadonlySet<string>;
  theme: ChartTheme;
  zoom: ZoomWindow;
  yRange: [number, number] | null;
  /** x index to mark (the point the user drilled into). */
  markIndex: number | null;
  /** Plot is narrow: drop the axis names and the slider's margins. */
  narrow: boolean;
}

/** Fixed order, so a series keeps its colour across redraws. */
const PALETTE = ['#0f766e', '#b45309', '#1d4ed8', '#9333ea', '#be123c', '#0e7490',
                 '#4d7c0f', '#a21caf', '#0369a1', '#c2410c', '#475569', '#15803d'];

/** Beyond the palette, walk the hue wheel by the golden angle: 20 series stay apart. */
export function paletteColour(index: number): string {
  if (index < PALETTE.length) return PALETTE[index];
  return `hsl(${Math.round(((index - PALETTE.length) * 137.508 + 20) % 360)} 58% 46%)`;
}

export function seriesColour(spec: ChartSpec, index: number): string {
  return spec.series[index]?.color || paletteColour(index);
}

export function initialView(spec: ChartSpec): ChartView {
  const kind = spec.kind ?? 'line';
  let type: ChartType = 'line';
  if (kind === 'bar') type = spec.stacked ? 'stacked-bar' : 'bar';
  else if (kind === 'scatter') type = 'scatter';
  else if (kind === 'area' || spec.stacked) type = spec.stacked ? 'stacked-area' : 'area';
  return { type, duration: false, log: false };
}

export function isHeatmap(spec: ChartSpec): boolean {
  return spec.kind === 'heatmap';
}

/** Most points x series the browser draws. Beyond it the chart is thinned (see thinSpec). */
export const MAX_DRAW_CELLS = 250_000;

/**
 * Cut an oversized chart to the draw budget with ONE shared set of x indices.
 *
 * Per bucket keep the x where the sum over all series is largest and where it is
 * smallest (plus the ends), so peaks survive and every series still lines up with
 * its labels and stacks still add up. ECharts' own `sampling` is not used: it picks
 * different points for each series, so an axis tooltip would show only the few
 * series that happen to have a point under the cursor, and a stack would not add up.
 *
 * Returns the spec unchanged (`keep` null) when it fits.
 */
export function thinSpec(spec: ChartSpec): { spec: ChartSpec; keep: number[] | null } {
  const n = spec.x.length;
  if (isHeatmap(spec) || n * Math.max(1, spec.series.length) <= MAX_DRAW_CELLS) return { spec, keep: null };
  const target = Math.max(4, Math.floor(MAX_DRAW_CELLS / spec.series.length));
  const totals = new Float64Array(n);
  spec.series.forEach((s) => s.values.forEach((v, i) => { if (v !== null) totals[i] += v; }));
  const bucket = Math.ceil(n / (target / 2));
  const keep = new Set<number>([0, n - 1]);
  for (let start = 0; start < n; start += bucket) {
    let hi = start;
    let lo = start;
    for (let i = start; i < Math.min(start + bucket, n); i += 1) {
      if (totals[i] > totals[hi]) hi = i;
      if (totals[i] < totals[lo]) lo = i;
    }
    keep.add(hi); keep.add(lo);
  }
  const idx = [...keep].sort((a, b) => a - b);
  return {
    keep: idx,
    spec: {
      ...spec,
      x: idx.map((i) => spec.x[i]),
      series: spec.series.map((s) => ({ ...s, values: idx.map((i) => s.values[i]) })),
      thinnedFrom: spec.thinnedFrom ?? n,
    },
  };
}

/** Sort descending, gaps last. */
export function sortedDescending(values: (number | null)[]): (number | null)[] {
  const present = values.filter((v): v is number => v !== null).sort((a, b) => b - a);
  return [...present, ...new Array(values.length - present.length).fill(null)];
}

/** The series as drawn under `view`: duration-sorted, log-safe. */
export function viewSeries(spec: ChartSpec, view: ChartView): ChartSeries[] {
  return spec.series.map((s) => {
    let values = view.duration ? sortedDescending(s.values) : s.values;
    // A log axis cannot place zero or a negative: leave a gap, and say so (the
    // component counts these for its note) rather than let ECharts draw nothing.
    if (view.log) values = values.map((v) => (v !== null && v > 0 ? v : null));
    return { ...s, values };
  });
}

export function hiddenOnLog(spec: ChartSpec): number {
  return spec.series.reduce((n, s) => n + s.values.filter((v) => v !== null && v <= 0).length, 0);
}

export function xLabels(spec: ChartSpec, view: ChartView): string[] {
  return view.duration ? spec.x.map((_, i) => String(i + 1)) : spec.x;
}

/** Drop a year every label shares: `2030-06-01 08:00` -> `06-01 08:00`. */
function shortLabels(labels: string[]): (value: string) => string {
  const m = /^(\d{4})-/.exec(labels[0] ?? '');
  if (!m || !labels.every((l) => l.startsWith(m[0]))) return (v) => v;
  return (v) => (v.startsWith(m[0]) ? v.slice(5) : v);
}

function effectiveType(s: ChartSeries, view: ChartView): 'line' | 'area' | 'bar' | 'scatter' {
  const base = view.type === 'stacked-area' ? 'area' : view.type === 'stacked-bar' ? 'bar' : view.type;
  const k = s.kind ?? base;
  return k === 'bar' ? 'bar' : k === 'area' ? 'area' : k === 'scatter' ? 'scatter' : 'line';
}

/**
 * Whether a series joins the stack.
 *
 * Never a right-axis series (stacking a price on MW is meaningless), never in
 * duration mode (each series is sorted on its own, so rank 1 of solar and rank 1
 * of gas are different hours and their sum is a quantity that never occurred), and
 * never on a log axis (a stack of logs is not a log of the stack).
 */
export function joinsStack(s: ChartSeries, view: ChartView): boolean {
  const stackedType = view.type === 'stacked-area' || view.type === 'stacked-bar';
  if (!stackedType || view.duration || view.log || s.axis === 'right') return false;
  return effectiveType(s, view) !== 'line' && effectiveType(s, view) !== 'scatter';
}

export function readTheme(): ChartTheme {
  const css = getComputedStyle(document.documentElement);
  const get = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  const surface = get('--surface', '#ffffff');
  const hex = /^#([0-9a-f]{6})$/i.exec(surface);
  const lum = hex
    ? (0.299 * parseInt(hex[1].slice(0, 2), 16) + 0.587 * parseInt(hex[1].slice(2, 4), 16)
       + 0.114 * parseInt(hex[1].slice(4, 6), 16)) / 255
    : 1;
  return {
    text: get('--text', '#1f2328'), muted: get('--muted', '#6b7280'), border: get('--border', '#e5e4df'),
    surface, brand: get('--brand', '#0f766e'), warn: get('--warn', '#b45309'), dark: lum < 0.5,
  };
}

interface TipParam {
  seriesName?: string;
  value?: unknown;
  marker?: string;
  axisValueLabel?: string;
  seriesIndex?: number;
  data?: unknown;
}

export function buildOption(p: ChartParams): EChartsOption {
  const { spec, view, hidden, theme, zoom, yRange, markIndex, narrow } = p;
  const { text, muted, border, surface } = theme;
  const tooltipBase = {
    confine: true, backgroundColor: surface, borderColor: border,
    textStyle: { color: text, fontSize: 12 }, extraCssText: 'max-width:min(560px,92%);',
  };

  if (isHeatmap(spec)) return heatmapOption(p, tooltipBase);

  const labels = xLabels(spec, view);
  const drawn = viewSeries(spec, view);
  const usesRight = spec.series.some((s) => s.axis === 'right' && !hidden.has(s.name));
  const showSlider = labels.length > 24;
  const short = shortLabels(labels);

  const visible = drawn
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => !hidden.has(s.name));
  let marked = false;
  const series: SeriesOption[] = visible.map(({ s, i }) => {
    const type = effectiveType(s, view);
    const stacked = joinsStack(s, view);
    const colour = seriesColour(spec, i);
    const many = labels.length > 2000;  // bars only: draw them in one batch
    const markLine = markIndex !== null && !marked && !view.duration
      ? {
          symbol: 'none', silent: true, animation: false, label: { show: false },
          lineStyle: { color: text, type: 'dashed' as const, width: 1 },
          data: [{ xAxis: markIndex }],
        }
      : undefined;
    if (markLine) marked = true;
    const base = {
      name: s.name, data: s.values, yAxisIndex: s.axis === 'right' ? 1 : 0,
      itemStyle: { color: colour }, markLine,
    };
    if (type === 'bar') {
      return { ...base, type: 'bar' as const, stack: stacked ? 'total' : undefined,
               barMaxWidth: 40, large: many, emphasis: { focus: 'series' as const } };
    }
    if (type === 'scatter') {
      return { ...base, type: 'scatter' as const, symbolSize: 6 };
    }
    return {
      ...base, type: 'line' as const,
      stack: stacked ? 'total' : undefined,
      // Few points: show them, so there is something to hover. Many: hide, the line is the data.
      showSymbol: labels.length <= 40, symbolSize: 5, connectNulls: false,
      lineStyle: { width: 1.6, color: colour },
      areaStyle: type === 'area' ? { color: colour, opacity: stacked ? 0.78 : 0.18 } : undefined,
      emphasis: { focus: 'series' as const },
    };
  });

  const compact = labels.length <= 6;
  const gridBottom = showSlider ? 34 : 24;
  const axisCommon = { axisLabel: { color: muted, fontSize: 10 }, axisLine: { lineStyle: { color: border } } };
  const yCommon = (name: string | undefined, right: boolean, show: boolean) => ({
    type: (view.log ? 'log' : 'value') as 'log' | 'value',
    name: narrow ? undefined : name, nameTextStyle: { color: muted, fontSize: 10, align: right ? 'right' as const : 'left' as const },
    axisLabel: { color: muted, fontSize: 10, formatter: (v: number) => fmtCompact(v) },
    axisLine: { show: false },
    splitLine: right ? { show: false } : { lineStyle: { color: border, type: 'dashed' as const } },
    scale: false, show,
  });

  return {
    textStyle: { color: text },
    animation: false,
    tooltip: {
      ...tooltipBase,
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: muted, formatter: (q: { value: unknown }) => fmtNumber(Number(q.value)) || String(q.value) } },
      formatter: (raw: unknown) => {
        const params = (Array.isArray(raw) ? raw : [raw]) as TipParam[];
        const rows = params.filter((q) => q.value !== null && q.value !== undefined && q.value !== '-');
        if (rows.length === 0) return '';
        const head = escapeHtml(rows[0].axisValueLabel ?? '');
        let stackTotal = 0;
        let stackCount = 0;
        const items = rows.map((q) => {
          const idx = visible.find(({ s }) => s.name === q.seriesName);
          const right = idx?.s.axis === 'right';
          const v = typeof q.value === 'number' ? q.value : Number(q.value);
          if (idx && joinsStack(idx.s, view) && Number.isFinite(v)) { stackTotal += v; stackCount += 1; }
          const unit = (right ? spec.unitRight : spec.unit) ?? '';
          return `<div class="chart-tip-row"><span class="chart-tip-name">${q.marker ?? ''}${escapeHtml(q.seriesName)}</span>`
            + `<span class="chart-tip-val">${escapeHtml(fmtNumber(v))}${unit ? ` ${escapeHtml(unit)}` : ''}</span></div>`;
        });
        const total = stackCount > 1
          ? `<div class="chart-tip-row chart-tip-total"><span class="chart-tip-name">Stack total</span>`
            + `<span class="chart-tip-val">${escapeHtml(fmtNumber(stackTotal))}${spec.unit ? ` ${escapeHtml(spec.unit)}` : ''}</span></div>`
          : '';
        return `<div class="chart-tip"><div class="chart-tip-head">${head}</div>`
          + `<div class="chart-tip-body${items.length > 10 ? ' is-wide' : ''}">${items.join('')}</div>${total}</div>`;
      },
    },
    grid: { left: 8, right: usesRight ? 8 : 14, top: narrow ? 14 : 28, bottom: gridBottom, containLabel: true },
    xAxis: {
      type: 'category', data: labels, boundaryGap: effectiveBoundary(view, visible.map(({ s }) => s)),
      axisLabel: { ...axisCommon.axisLabel, hideOverlap: true, formatter: (v: string) => short(v) },
      axisLine: axisCommon.axisLine, axisTick: { alignWithLabel: true },
      name: view.duration && !narrow ? 'rank' : undefined, nameTextStyle: { color: muted, fontSize: 10 },
    },
    yAxis: [
      { ...yCommon(spec.unit, false, true), ...(yRange ? { min: yRange[0], max: yRange[1] } : {}) },
      yCommon(spec.unitRight, true, usesRight),
    ],
    dataZoom: labels.length > 1 && !compact
      ? [{ type: 'inside', xAxisIndex: 0, start: zoom.start, end: zoom.end, throttle: 30, filterMode: 'filter' },
         ...(showSlider ? [{
           type: 'slider' as const, xAxisIndex: 0, start: zoom.start, end: zoom.end, height: 16, bottom: 6,
           showDetail: false, borderColor: border, brushSelect: false,
           fillerColor: `${theme.brand}33`, textStyle: { color: muted, fontSize: 9 },
         }] : [])]
      : [],
    series,
  };
}

function effectiveBoundary(view: ChartView, series: ChartSeries[]): boolean {
  return series.some((s) => effectiveType(s, view) === 'bar');
}

function heatmapOption(p: ChartParams, tooltipBase: Record<string, unknown>): EChartsOption {
  const { spec, theme, zoom } = p;
  const { text, muted, border } = theme;
  const data: [number, number, number][] = [];
  let min = Infinity;
  let max = -Infinity;
  spec.series.forEach((s, row) => s.values.forEach((v, col) => {
    if (v === null) return;
    data.push([col, row, v]);
    if (v < min) min = v;
    if (v > max) max = v;
  }));
  const rows = spec.series.map((s) => s.name);
  const short = shortLabels(spec.x);
  return {
    textStyle: { color: text },
    animation: false,
    tooltip: {
      ...tooltipBase, position: 'top',
      formatter: (raw: unknown) => {
        const q = raw as TipParam;
        const d = (q.data ?? []) as number[];
        const unit = spec.unit ? ` ${escapeHtml(spec.unit)}` : '';
        return `<div class="chart-tip"><div class="chart-tip-head">${escapeHtml(rows[d[1]])} at ${escapeHtml(spec.x[d[0]])}</div>`
          + `<div class="chart-tip-row"><span class="chart-tip-name">Value</span><span class="chart-tip-val">${escapeHtml(fmtNumber(d[2]))}${unit}</span></div></div>`;
      },
    },
    grid: { left: 8, right: 14, top: 12, bottom: 72, containLabel: true },
    xAxis: { type: 'category', data: spec.x, splitArea: { show: false },
             axisLabel: { color: muted, fontSize: 10, hideOverlap: true, formatter: (v: string) => short(v) },
             axisLine: { lineStyle: { color: border } } },
    yAxis: { type: 'category', data: rows, inverse: true,
             axisLabel: { color: muted, fontSize: 10, hideOverlap: true }, axisLine: { lineStyle: { color: border } } },
    visualMap: {
      min: Number.isFinite(min) ? min : 0, max: Number.isFinite(max) ? max : 1,
      calculable: true, orient: 'horizontal', left: 'center', bottom: 4, itemHeight: 110, itemWidth: 11,
      textStyle: { color: muted, fontSize: 10 }, formatter: (v: unknown) => fmtCompact(Number(v)),
      inRange: { color: [theme.surface, theme.brand, theme.warn] },
    },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, start: zoom.start, end: zoom.end, throttle: 30 },
      { type: 'inside', yAxisIndex: 0, zoomOnMouseWheel: 'shift', moveOnMouseMove: 'shift', throttle: 30 },
    ],
    series: [{
      type: 'heatmap', data, progressive: 4000,
      emphasis: { itemStyle: { borderColor: text, borderWidth: 1 } },
    }],
  };
}

/** CSV of the data as drawn: rank-sorted when the duration curve is on. */
export function toCsv(spec: ChartSpec, view: ChartView): string {
  const lines: string[] = [];
  if (isHeatmap(spec)) {
    lines.push(['series', ...spec.x].map(csvCell).join(','));
    spec.series.forEach((s) => lines.push([s.name, ...s.values].map(csvCell).join(',')));
    return lines.join('\n');
  }
  const drawn = viewSeries(spec, { ...view, log: false });
  lines.push([view.duration ? 'rank' : 'x', ...drawn.map((s) => s.name)].map(csvCell).join(','));
  xLabels(spec, view).forEach((label, i) => {
    lines.push([label, ...drawn.map((s) => s.values[i] ?? null)].map(csvCell).join(','));
  });
  return lines.join('\n');
}

/** What every series was at x index `i`, for the drill-in panel. */
export function valuesAt(spec: ChartSpec, i: number): { name: string; value: number; colour: string; axis?: string }[] {
  if (isHeatmap(spec)) {
    return spec.series.map((s, row) => ({ name: s.name, value: s.values[i] as number, colour: seriesColour(spec, row) }))
      .filter((r) => r.value !== null && r.value !== undefined);
  }
  return spec.series
    .map((s, idx) => ({ name: s.name, value: s.values[i], colour: seriesColour(spec, idx), axis: s.axis }))
    .filter((r): r is { name: string; value: number; colour: string; axis: string | undefined } => r.value !== null && r.value !== undefined);
}
