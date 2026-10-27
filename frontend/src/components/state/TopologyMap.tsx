/**
 * The network as a map you can click.
 *
 * Buses sit at their own coordinates when the model has them (x = longitude, y =
 * latitude, equirectangular, so a degree of longitude is drawn cos(lat) as wide as a
 * degree of latitude); a model with none gets a circular layout, which at least
 * shows what connects to what. Wheel zooms about the pointer, drag pans, a click on
 * a bus or a line selects it. Hand-rolled SVG: there is no tile layer to fetch, and
 * a grid's topology reads better as a graph than over a basemap.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { TopologyBranch, TopologyBus } from '../../api';

interface Props {
  buses: TopologyBus[];
  branches: TopologyBranch[];
  hasCoordinates: boolean;
  /** Colour of a carrier, by name (the model's own, else the shared palette). */
  colourOf: (carrier: string) => string;
  selectedBus: string | null;
  selectedBranch: string | null;
  onSelectBus: (name: string | null) => void;
  onSelectBranch: (name: string | null) => void;
}

interface Placed { bus: TopologyBus; x: number; y: number }

const PAD = 36;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 40;

function layout(buses: TopologyBus[], hasCoordinates: boolean, w: number, h: number): Placed[] {
  if (buses.length === 0) return [];
  if (!hasCoordinates) {
    const r = Math.max(20, Math.min(w, h) / 2 - PAD);
    return buses.map((bus, i) => {
      const a = (i / buses.length) * Math.PI * 2 - Math.PI / 2;
      return { bus, x: w / 2 + (buses.length === 1 ? 0 : r * Math.cos(a)), y: h / 2 + (buses.length === 1 ? 0 : r * Math.sin(a)) };
    });
  }
  const located = buses.filter((b) => b.x !== null && b.y !== null);
  const meanLat = located.reduce((s, b) => s + (b.y as number), 0) / Math.max(1, located.length);
  const kx = Math.cos((meanLat * Math.PI) / 180);
  const xs = located.map((b) => (b.x as number) * kx);
  const ys = located.map((b) => b.y as number);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const span = Math.max(x1 - x0, y1 - y0, 1e-9);
  const scale = (Math.min(w, h) - 2 * PAD) / span;
  const ox = (w - (x1 - x0) * scale) / 2;
  const oy = (h - (y1 - y0) * scale) / 2;
  return buses.map((bus) => {
    const has = bus.x !== null && bus.y !== null;
    return {
      bus,
      x: has ? ox + ((bus.x as number) * kx - x0) * scale : PAD,
      // SVG y grows downward; latitude grows upward.
      y: has ? oy + (y1 - (bus.y as number)) * scale : PAD,
    };
  });
}

export function TopologyMap(props: Props) {
  const { buses, branches, selectedBus, selectedBranch } = props;
  const hostRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 480, h: 320 });
  const [view, setView] = useState({ k: 1, tx: 0, ty: 0 });
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    const observer = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width > 0) setSize({ w: Math.round(r.width), h: Math.max(220, Math.round(Math.min(r.width * 0.7, 420))) });
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const placed = useMemo(() => layout(buses, props.hasCoordinates, size.w, size.h), [buses, props.hasCoordinates, size]);
  const at = useMemo(() => new Map(placed.map((p) => [p.bus.name, p])), [placed]);
  const maxCap = Math.max(1, ...branches.map((b) => b.capacity_mw ?? 0));
  const maxAssets = Math.max(1, ...buses.map((b) => b.assets_total));

  // Re-fit when the topology itself changes (a different model), not on every edit.
  const shape = `${buses.length}:${branches.length}:${props.hasCoordinates}`;
  useEffect(() => setView({ k: 1, tx: 0, ty: 0 }), [shape]);

  const onWheel = (e: React.WheelEvent) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    setView((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * (e.deltaY < 0 ? 1.18 : 1 / 1.18)));
      const f = k / v.k;
      return { k, tx: px - (px - v.tx) * f, ty: py - (py - v.ty) * f };
    });
  };
  const dominant = (bus: TopologyBus): string => {
    const by = new Map<string, number>();
    bus.assets.forEach((a) => { if (a.carrier) by.set(a.carrier, (by.get(a.carrier) ?? 0) + (a.p_nom_mw ?? 1)); });
    let best = '';
    by.forEach((v, c) => { if (!best || v > (by.get(best) ?? 0)) best = c; });
    return best;
  };

  return (
    <div className="topo" ref={hostRef}>
      <svg width={size.w} height={size.h} role="img" aria-label="Network topology"
        onWheel={onWheel}
        onPointerDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, moved: false }; }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (!d) return;
          const dx = e.clientX - d.x;
          const dy = e.clientY - d.y;
          // Capture only once it is a drag: capturing at pointerdown would retarget the
          // click to the <svg> and a bus could never be selected.
          if (!d.moved && Math.abs(dx) + Math.abs(dy) > 3) { d.moved = true; (e.currentTarget as Element).setPointerCapture(e.pointerId); }
          if (d.moved) { setView((v) => ({ ...v, tx: v.tx + dx, ty: v.ty + dy })); d.x = e.clientX; d.y = e.clientY; }
        }}
        onPointerUp={() => { window.setTimeout(() => { drag.current = null; }, 0); }}
        onClick={() => { if (!drag.current?.moved) { props.onSelectBus(null); props.onSelectBranch(null); } }}>
        <g transform={`translate(${view.tx} ${view.ty}) scale(${view.k})`}>
          {branches.map((br) => {
            const a = at.get(br.bus0);
            const b = at.get(br.bus1);
            if (!a || !b) return null;
            const on = br.name === selectedBranch;
            return (
              <g key={`${br.sheet}:${br.name}`} className={`topo-branch${on ? ' is-selected' : ''}`}
                onClick={(e) => { e.stopPropagation(); props.onSelectBranch(on ? null : br.name); props.onSelectBus(null); }}>
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="topo-hit" strokeWidth={12 / view.k} />
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="topo-line"
                  strokeWidth={(1.2 + 3.2 * ((br.capacity_mw ?? 0) / maxCap)) / view.k} />
                <title>{`${br.name} (${br.sheet}): ${br.bus0} to ${br.bus1}${br.capacity_mw !== null ? `, ${br.capacity_mw} MW` : ''}`}</title>
              </g>
            );
          })}
          {placed.map(({ bus, x, y }) => {
            const on = bus.name === selectedBus;
            const r = (7 + 9 * Math.sqrt(bus.assets_total / maxAssets)) / view.k;
            return (
              <g key={bus.name} className={`topo-bus${on ? ' is-selected' : ''}`} transform={`translate(${x} ${y})`}
                onClick={(e) => { e.stopPropagation(); props.onSelectBus(on ? null : bus.name); props.onSelectBranch(null); }}>
                <circle r={r} fill={props.colourOf(dominant(bus))} strokeWidth={(on ? 3 : 1.5) / view.k} />
                {(view.k >= 0.8 || buses.length <= 14) && (
                  <text y={r + 11 / view.k} fontSize={11 / view.k} textAnchor="middle">{bus.name}</text>
                )}
                <title>{`${bus.name}: ${bus.assets_total} attached asset${bus.assets_total === 1 ? '' : 's'}`}</title>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="topo-tools">
        <button className="btn-ghost sm" onClick={() => setView((v) => ({ ...v, k: Math.min(MAX_ZOOM, v.k * 1.4) }))} aria-label="Zoom in">Zoom in</button>
        <button className="btn-ghost sm" onClick={() => setView((v) => ({ ...v, k: Math.max(MIN_ZOOM, v.k / 1.4) }))} aria-label="Zoom out">Zoom out</button>
        <button className="btn-ghost sm" onClick={() => setView({ k: 1, tx: 0, ty: 0 })}>Fit</button>
      </div>
      {!props.hasCoordinates && buses.length > 1 && (
        <p className="muted topo-note">These buses have no coordinates, so they are laid out in a circle.</p>
      )}
    </div>
  );
}
