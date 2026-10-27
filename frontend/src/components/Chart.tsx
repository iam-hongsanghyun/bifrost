/**
 * The one chart component: the transcript, the State panel's dashboard and the
 * full-screen view all render this, so a fix lands everywhere.
 *
 * What was wrong with the version this replaces (found by running chart.show
 * payloads through it in a browser):
 *
 *  - Every streamed token re-rendered the transcript, which rebuilt each chart's
 *    spec object, which rebuilt the ECharts option and replaced it: zoom and legend
 *    selection reset mid-answer. Specs are now cached per event, the component is
 *    memoised, and zoom lives in a ref.
 *  - Long series were thinned once, in place, to a few thousand min/max pairs, so
 *    zooming in showed the same points larger and the tooltip's labels no longer
 *    matched the data. Every point is now drawn (up to a budget of 250,000 values);
 *    only beyond that is the chart thinned, with shared indices that keep peaks.
 *  - The legend was a canvas strip that scrolled 9 of 20 entries out of reach, could
 *    not isolate a series and was reset on every redraw. It is now an HTML legend:
 *    click toggles, double-click isolates, hover highlights.
 *  - Resize followed the window only; the split panel resizing left the chart at its
 *    old size. A ResizeObserver now follows the element.
 *  - Theme was read once from the OS and its colours were literals. It follows the
 *    page's own custom properties and re-reads when the scheme changes.
 *  - Tooltips are HTML and interpolated series names unescaped.
 *  - A missing value was dropped from a series, shifting the rest against x.
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { init, use } from 'echarts/core';
import type { ECharts } from 'echarts/core';
import { BarChart, HeatmapChart, LineChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomComponent, GridComponent, MarkLineComponent, TooltipComponent, VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import {
  buildOption, CHART_TYPES, ChartSpec, ChartType, ChartView, hiddenOnLog, initialView, isHeatmap, joinsStack,
  readTheme, seriesColour, thinSpec, toCsv, valuesAt, ZoomWindow,
} from '../lib/chartModel';
import { downloadText, downloadUrl, fmtNumber, safeFilename } from '../lib/format';

export type { ChartSpec } from '../lib/chartModel';

use([
  LineChart, BarChart, ScatterChart, HeatmapChart,
  GridComponent, TooltipComponent, DataZoomComponent, VisualMapComponent, MarkLineComponent,
  CanvasRenderer,
]);

export interface DrillTarget {
  /** Index into the x the chart DREW (thinned, when it was too big). */
  index: number;
  /** Index into the spec's own x: what the caller supplied, whatever was drawn. */
  sourceIndex: number;
  label: string;
}

export interface ChartProps {
  spec: ChartSpec;
  /** Plot height in px; the full-screen view fills the window instead. */
  height?: number;
  /** Fires when the user clicks a time point (or a bar, or a heatmap cell). */
  onDrill?: (target: DrillTarget) => void;
  /** Extra content in the drill-in panel, e.g. the dashboard's per-asset breakdown. */
  drillExtra?: (target: DrillTarget) => React.ReactNode;
  pinned?: boolean;
  onTogglePin?: () => void;
  /** Panel layout: fill the width instead of the transcript's reading measure. */
  wide?: boolean;
}

const FULL_ZOOM: ZoomWindow = { start: 0, end: 100 };
const CLICK_SLOP_PX = 4;

/** Re-read the page's theme when the OS scheme flips; one subscription per chart. */
function useThemeTick(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!mq) return undefined;
    const on = () => setTick((n) => n + 1);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return tick;
}

/** Everything the user can change on one chart, shared by its inline and full-screen views. */
function useChartState(spec: ChartSpec) {
  const [view, setView] = useState<ChartView>(() => initialView(spec));
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [yRange, setYRange] = useState<[number, number] | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const [drill, setDrill] = useState<number | null>(null);
  const zoomRef = useRef<ZoomWindow>({ ...FULL_ZOOM });

  // The agent redrew the same slot with different data: drop state tied to the old one.
  const key = spec.chartId ?? spec.title ?? '';
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    setHidden(new Set());
    setYRange(null);
    setDrill(null);
    setZoomed(false);
    zoomRef.current = { ...FULL_ZOOM };
    setView(initialView(spec));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const patchView = useCallback((patch: Partial<ChartView>) => {
    // time <-> rank changes what x means: the zoom window and the marked point no longer apply.
    if ('duration' in patch) {
      zoomRef.current = { ...FULL_ZOOM };
      setZoomed(false); setDrill(null); setYRange(null);
    }
    setView((v) => ({ ...v, ...patch }));
  }, []);
  const toggle = useCallback((name: string) => setHidden((h) => {
    const next = new Set(h);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  }), []);
  // Isolate; a second double-click on the already-isolated series brings everything back.
  const isolate = useCallback((name: string) => setHidden((h) => {
    const others = spec.series.map((s) => s.name).filter((n) => n !== name);
    const already = !h.has(name) && others.every((n) => h.has(n));
    return already ? new Set() : new Set(others);
  }), [spec.series]);

  return { view, patchView, hidden, setHidden, toggle, isolate, yRange, setYRange, zoomed, setZoomed, drill, setDrill, zoomRef };
}
type ChartState = ReturnType<typeof useChartState>;

interface FrameProps {
  spec: ChartSpec;
  st: ChartState;
  boxMode: boolean;
  heightCss: string;
  chartRef: React.MutableRefObject<ECharts | null>;
  onBox: (xRange: [number, number] | null, yRange: [number, number] | null) => void;
  onPoint: (index: number) => void;
}

/** The canvas: owns the ECharts instance, its resize and its pointer handling. */
function ChartFrame(props: FrameProps) {
  const { spec, st, boxMode, heightCss, chartRef } = props;
  const { view, hidden, yRange, drill, zoomRef } = st;
  const hostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const themeTick = useThemeTick();
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  // Callbacks go through a ref so the zrender listeners are bound once, not on every render.
  const cb = useRef({ props, boxMode, spec, view });
  cb.current = { props, boxMode, spec, view };
  const narrow = width > 0 && width < 460;

  const option = useMemo(
    () => buildOption({
      spec, view, hidden, theme: readTheme(), zoom: zoomRef.current, yRange, markIndex: drill, narrow,
    }),
    // zoomRef is read, not watched: zoom changes must not rebuild the option.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec, view, hidden, yRange, drill, narrow, themeTick],
  );

  // Create once; dispose on unmount. ResizeObserver, not the window event: the split
  // panel, the full-screen view and the rail all change the element's size without
  // the window changing at all.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    const chart = init(host, undefined, { renderer: 'canvas' });
    chartRef.current = chart;
    let frame = 0;
    const observer = new ResizeObserver((entries) => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const w = Math.round(entries[0]?.contentRect.width ?? host.clientWidth);
        setWidth(w);
        if (w > 0) chart.resize();
      });
    });
    observer.observe(host);

    chart.on('datazoom', () => {
      const dz = (chart.getOption().dataZoom ?? []) as { start?: number; end?: number }[];
      const first = dz[0];
      if (first && typeof first.start === 'number' && typeof first.end === 'number') {
        zoomRef.current = { start: first.start, end: first.end };
        st.setZoomed(first.start > 0.01 || first.end < 99.99);
      }
    });
    // Heatmap cell: the series click carries [col, row, value].
    chart.on('click', (params) => {
      if (params.componentSubType === 'heatmap' && Array.isArray(params.data)) {
        cb.current.props.onPoint(Number(params.data[0]));
      }
    });
    // Anywhere in the plot area: nearest x. A time series is read by x, and asking
    // the user to hit a 1-pixel line to drill in is how a chart ends up not clickable.
    const zr = chart.getZr();
    let down: [number, number] | null = null;
    zr.on('mousedown', (e) => { down = [e.offsetX, e.offsetY]; });
    zr.on('click', (e) => {
      const c = cb.current;
      if (c.boxMode || isHeatmap(c.spec) || c.view.duration) return;
      if (down && Math.hypot(e.offsetX - down[0], e.offsetY - down[1]) > CLICK_SLOP_PX) return;
      const pt: [number, number] = [e.offsetX, e.offsetY];
      if (!chart.containPixel('grid', pt)) return;
      // A single-axis finder takes (and returns) a scalar; only a grid finder takes a point.
      const idx = Math.round(Number(chart.convertFromPixel({ xAxisIndex: 0 }, pt[0])));
      if (Number.isFinite(idx) && idx >= 0 && idx < c.spec.x.length) c.props.onPoint(idx);
    });
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
    // st's setters are stable; the instance must be created exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true });
  }, [option, chartRef]);

  // Box zoom: an overlay owns the drag, so it cannot fight the chart's own pan.
  const clampPoint = (e: React.PointerEvent): [number, number] => {
    const r = hostRef.current!.getBoundingClientRect();
    return [Math.min(Math.max(e.clientX - r.left, 0), r.width), Math.min(Math.max(e.clientY - r.top, 0), r.height)];
  };
  const onDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    const [x, y] = clampPoint(e);
    setBox({ x0: x, y0: y, x1: x, y1: y });
  };
  const onMove = (e: React.PointerEvent) => {
    if (!box) return;
    const [x, y] = clampPoint(e);
    setBox({ ...box, x1: x, y1: y });
  };
  const onUp = () => {
    const chart = chartRef.current;
    const b = box;
    setBox(null);
    if (!chart || !b || Math.abs(b.x1 - b.x0) < 6 || Math.abs(b.y1 - b.y0) < 6) return;
    const n = spec.x.length;
    const toIdx = (px: number) =>
      Math.min(Math.max(Math.round(Number(chart.convertFromPixel({ xAxisIndex: 0 }, px))), 0), n - 1);
    const a = toIdx(Math.min(b.x0, b.x1));
    const z = toIdx(Math.max(b.x0, b.x1));
    const ys = [b.y0, b.y1].map((py) => Number(chart.convertFromPixel({ yAxisIndex: 0 }, py)));
    const yOk = Number.isFinite(ys[0]) && Number.isFinite(ys[1]) && ys[0] !== ys[1];
    props.onBox(a < z ? [a, z] : null, yOk ? [Math.min(ys[0], ys[1]), Math.max(ys[0], ys[1])] : null);
  };

  return (
    <div className="chart-plot" style={{ height: heightCss }}>
      <div ref={hostRef} className="chart-canvas" role="img" aria-label={spec.title || 'chart'} />
      {boxMode && (
        <div className="chart-box-layer" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp}>
          {box && (
            <div className="chart-box" style={{
              left: Math.min(box.x0, box.x1), top: Math.min(box.y0, box.y1),
              width: Math.abs(box.x1 - box.x0), height: Math.abs(box.y1 - box.y0),
            }} />
          )}
        </div>
      )}
    </div>
  );
}

/** What every series was at the clicked point — the generic drill-in. */
function DrillPanel(
  { spec, index, stack, onClose, extra }:
  { spec: ChartSpec; index: number; stack: ReadonlySet<string>; onClose: () => void; extra?: React.ReactNode },
) {
  const rows = valuesAt(spec, index).sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 1e-9);
  // Only what is actually stacked adds up: a load line or a price on the right axis does not.
  const stacked = rows.filter((r) => stack.has(r.name));
  const total = stacked.reduce((s, r) => s + r.value, 0);
  return (
    <div className="chart-drill">
      <div className="chart-drill-head">
        <strong>{isHeatmap(spec) ? `Column ${spec.x[index]}` : spec.x[index]}</strong>
        <span className="muted">
          {rows.length} series{stacked.length > 1 ? `, stack total ${fmtNumber(total)} ${spec.unit ?? ''}` : ''}
        </span>
        <button className="btn-ghost sm" onClick={onClose}>Close</button>
      </div>
      <div className="chart-drill-rows">
        {rows.slice(0, 40).map((r) => (
          <div key={r.name} className="chart-drill-row">
            <span className="chart-drill-name" title={r.name}>
              <i className="swatch" style={{ background: r.colour }} />{r.name}
            </span>
            <span className="chart-drill-bar"><i style={{ width: `${(Math.abs(r.value) / max) * 100}%`, background: r.colour }} /></span>
            <span className="chart-drill-val">{fmtNumber(r.value)}{spec.unit ? ` ${spec.unit}` : ''}</span>
          </div>
        ))}
        {rows.length > 40 && <div className="muted">and {rows.length - 40} more</div>}
      </div>
      {extra}
    </div>
  );
}

interface InnerProps extends ChartProps {
  st: ChartState;
  keep: number[] | null;
  expanded?: boolean;
  onExpand?: () => void;
  onClose?: () => void;
  /** The full-screen view is open elsewhere: do not draw a second canvas. */
  suspended?: boolean;
  onPointExternal: (index: number) => void;
}

function ChartInner(props: InnerProps) {
  const { spec, st, wide, expanded, suspended, pinned, onTogglePin, onExpand, onClose } = props;
  const { view, hidden, yRange, zoomed, drill } = st;
  const heatmap = isHeatmap(spec);
  const [boxMode, setBoxMode] = useState(false);
  const chartRef = useRef<ECharts | null>(null);

  const resetZoom = () => {
    st.zoomRef.current = { start: 0, end: 100 };
    st.setYRange(null);
    st.setZoomed(false);
    chartRef.current?.dispatchAction({ type: 'dataZoom', start: 0, end: 100 });
  };
  const onBox = useCallback((xr: [number, number] | null, yr: [number, number] | null) => {
    if (xr) chartRef.current?.dispatchAction({ type: 'dataZoom', startValue: xr[0], endValue: xr[1] });
    if (yr) st.setYRange(yr);
    if (xr || yr) st.setZoomed(true);
    setBoxMode(false);
  }, [st]);

  const names = spec.series.map((s) => s.name);
  const logLost = view.log ? hiddenOnLog(spec) : 0;
  const heightCss = expanded ? 'max(320px, calc(100vh - 260px))' : `${props.height ?? 300}px`;
  const png = () => {
    const url = chartRef.current?.getDataURL({ pixelRatio: 2, backgroundColor: readTheme().surface });
    if (url) downloadUrl(`${safeFilename(spec.title ?? '', 'chart')}.png`, url);
  };
  const csv = () => downloadText(`${safeFilename(spec.title ?? '', 'chart')}.csv`, toCsv(spec, view));
  const drillTarget = drill !== null && drill < spec.x.length
    ? { index: drill, sourceIndex: props.keep ? props.keep[drill] : drill, label: spec.x[drill] } : null;

  return (
    <figure className={`chart${wide ? ' chart--wide' : ''}${expanded ? ' chart--expanded' : ''}`}>
      <div className="chart-head">
        {spec.title && <figcaption className="chart-title">{spec.title}</figcaption>}
        {spec.thinnedFrom && (
          <span className="chip chip--warn"
            title="The chart was too large for the event stream, so each stretch kept its peak and trough.">
            {spec.x.length.toLocaleString()} of {spec.thinnedFrom.toLocaleString()} points
          </span>
        )}
      </div>
      <div className="chart-tools" role="toolbar" aria-label="Chart controls">
        {!heatmap && (
          <select className="chart-select" aria-label="Chart type" value={view.type}
            onChange={(e) => st.patchView({ type: e.target.value as ChartType })}>
            {spec.kind === 'scatter' && <option value="scatter">Scatter</option>}
            {CHART_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        )}
        {!heatmap && (
          <button className={`btn-ghost${view.duration ? ' is-on' : ''}`} aria-pressed={view.duration}
            onClick={() => st.patchView({ duration: !view.duration })}
            title="Sort every series high to low: read by rank instead of by time">
            Duration curve
          </button>
        )}
        {!heatmap && (
          <button className={`btn-ghost${view.log ? ' is-on' : ''}`} aria-pressed={view.log}
            onClick={() => st.patchView({ log: !view.log })}
            title="Logarithmic y axis (values at or below zero become gaps)">
            Log y
          </button>
        )}
        {!heatmap && (
          <button className={`btn-ghost${boxMode ? ' is-on' : ''}`} aria-pressed={boxMode}
            onClick={() => setBoxMode((b) => !b)}
            title="Drag a box on the plot to zoom to it. Scroll to zoom, drag to pan.">
            Box zoom
          </button>
        )}
        <button className="btn-ghost" onClick={resetZoom} disabled={!zoomed && !yRange}>Reset zoom</button>
        <span className="chart-tools-gap" />
        <button className="btn-ghost" onClick={png} title="Save the chart as a PNG">PNG</button>
        <button className="btn-ghost" onClick={csv} title="Save the data as CSV">CSV</button>
        {onTogglePin && (
          <button className={`btn-ghost${pinned ? ' is-on' : ''}`} aria-pressed={pinned} onClick={onTogglePin}
            title="Keep this chart in the State panel">
            {pinned ? 'Unpin' : 'Pin'}
          </button>
        )}
        {expanded
          ? <button className="btn-ghost" onClick={onClose}>Close</button>
          : <button className="btn-ghost" onClick={onExpand} title="Open full screen">Expand</button>}
      </div>

      {!heatmap && names.length > 1 && (
        <div className="chart-legend" role="group" aria-label="Series">
          {names.map((name, i) => (
            <button key={name} className={`chart-legend-item${hidden.has(name) ? ' is-off' : ''}`}
              aria-pressed={!hidden.has(name)}
              onClick={() => st.toggle(name)} onDoubleClick={() => st.isolate(name)}
              onMouseEnter={() => chartRef.current?.dispatchAction({ type: 'highlight', seriesName: name })}
              onMouseLeave={() => chartRef.current?.dispatchAction({ type: 'downplay', seriesName: name })}
              title="Click to hide or show. Double-click to show only this series.">
              <i className="swatch" style={{ background: seriesColour(spec, i) }} />{name}
            </button>
          ))}
          {hidden.size > 0 && <button className="btn-ghost sm" onClick={() => st.setHidden(new Set())}>Show all</button>}
        </div>
      )}

      {suspended ? (
        <div className="chart-plot chart-plot--empty" style={{ height: heightCss }}>
          <span className="muted">Open full screen.</span>
        </div>
      ) : (
        <ChartFrame spec={spec} st={st} boxMode={boxMode} heightCss={heightCss} chartRef={chartRef}
          onBox={onBox} onPoint={props.onPointExternal} />
      )}

      {drillTarget && (
        <DrillPanel spec={spec} index={drillTarget.index} onClose={() => st.setDrill(null)}
          stack={new Set(spec.series.filter((s) => joinsStack(s, view)).map((s) => s.name))}
          extra={props.drillExtra?.(drillTarget)} />
      )}
      {spec.note && <p className="chart-note">{spec.note}</p>}
      {view.duration && (
        <p className="chart-note">
          Sorted high to low: the x axis is rank, not time. Each series is sorted on its own, so a given
          rank is a different hour in each
          {(spec.stacked || view.type.startsWith('stacked')) ? ', and stacking is off because that sum never occurred' : ''}.
        </p>
      )}
      {logLost > 0 && (
        <p className="chart-note">{logLost.toLocaleString()} values at or below zero are not drawn on a log axis.</p>
      )}
      {!heatmap && !drillTarget && spec.x.length > 1 && !view.duration && (
        <p className="chart-hint muted">Click the plot to break down a point. Scroll to zoom, drag to pan.</p>
      )}
    </figure>
  );
}

/**
 * The chart. `spec` must be referentially stable for the same content (the
 * transcript caches it per event); `memo` then keeps a streaming token elsewhere on
 * the page from touching it at all.
 */
export const Chart = memo(function Chart(props: ChartProps) {
  // Over the draw budget the chart is thinned; everything below sees the thinned spec.
  const thin = useMemo(() => thinSpec(props.spec), [props.spec]);
  const spec = thin.spec;
  const st = useChartState(spec);
  const [full, setFull] = useState(false);
  const { onDrill } = props;
  const keep = thin.keep;
  const point = useCallback((index: number) => {
    st.setDrill(index);
    onDrill?.({ index, sourceIndex: keep ? keep[index] : index, label: spec.x[index] });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onDrill, spec.x, keep, st.setDrill]);

  useEffect(() => {
    if (!full) return undefined;
    const on = (e: KeyboardEvent) => { if (e.key === 'Escape') setFull(false); };
    window.addEventListener('keydown', on);
    return () => window.removeEventListener('keydown', on);
  }, [full]);

  return (
    <>
      <ChartInner {...props} spec={spec} keep={keep} st={st} suspended={full} onExpand={() => setFull(true)} onPointExternal={point} />
      {full && createPortal(
        <div className="chart-modal" role="dialog" aria-modal="true" aria-label={spec.title || 'Chart'}
          onMouseDown={(e) => { if (e.target === e.currentTarget) setFull(false); }}>
          <div className="chart-modal-body">
            <ChartInner {...props} spec={spec} keep={keep} st={st} expanded wide onClose={() => setFull(false)} onPointExternal={point} />
          </div>
        </div>,
        document.body,
      )}
    </>
  );
});
