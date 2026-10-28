/**
 * Results: an interactive dashboard for one run, or two to five side by side.
 *
 * Every panel is the shared Chart, so zoom, legend isolation, chart-type switch,
 * duration curve, log axis, full screen and exports work the same here as in the
 * transcript. Selectors (bus, carrier, period) re-query Bifrost, which narrows the
 * data server-side. Clicking a time point on any time-based chart opens that
 * snapshot's dispatch by carrier, asset and bus.
 */
import { useEffect, useMemo, useState } from 'react';
import { Comparison, Dashboard, ListResponse, RunMeta, SnapshotDrill, stateApi } from '../../api';
import { Chart, DrillTarget } from '../Chart';
import { categorySpec, dayHourHeatmap, timeSpec, withCurtailment } from '../../lib/derive';
import { ChartSpec } from '../../lib/chartModel';
import { fmtNumber } from '../../lib/format';
import { Resource, useResource } from '../../lib/useResource';

interface Props {
  runs: Resource<ListResponse<RunMeta>>;
  selected: string | null;
  onSelect: (name: string) => void;
  compare: string[];
  onCompare: (names: string[]) => void;
}

function Kpis({ items }: { items: Dashboard['kpis'] }) {
  return (
    <div className="kpi-grid">
      {items.map((k) => (
        <div key={k.key} className="kpi">
          <span className="kpi-label">{k.label}</span>
          <span className="kpi-value">
            {k.key === 'totalCost' && k.value !== null ? `${k.unit}${fmtNumber(k.value, 0)}`
              : k.value !== null ? `${fmtNumber(k.value, Math.abs(k.value) >= 100 ? 0 : 2)}${k.unit ? ` ${k.unit}` : ''}`
                : (k.text ?? 'n/a')}
          </span>
        </div>
      ))}
    </div>
  );
}

/** One snapshot, opened by clicking a time point: who generated what, and where. */
function SnapshotBreakdown({ run, index, bus }: { run: string; index: number; bus: string | null }) {
  const snap = useResource<SnapshotDrill>(() => stateApi.snapshot(run, index, bus), [run, index, bus]);
  const d = snap.data;
  if (snap.error) return <p className="panel-error">{snap.error}</p>;
  if (!d) return <p className="muted">Loading the snapshot.</p>;
  const max = Math.max(1e-9, ...d.by_carrier.map((c) => Math.abs(c.p_mw)));
  return (
    <div className="snapshot">
      <h4 className="panel-h">Dispatch at {d.label}</h4>
      <div className="chart-drill-rows">
        {d.by_carrier.map((c) => (
          <div key={c.name} className="chart-drill-row">
            <span className="chart-drill-name"><i className="swatch" style={{ background: c.color ?? 'var(--brand)' }} />{c.name}</span>
            <span className="chart-drill-bar"><i style={{ width: `${(Math.abs(c.p_mw) / max) * 100}%`, background: c.color ?? 'var(--brand)' }} /></span>
            <span className="chart-drill-val">{fmtNumber(c.p_mw)} MW</span>
          </div>
        ))}
      </div>
      {d.load_mw !== null && <p className="muted">Load {fmtNumber(d.load_mw)} MW</p>}
      {d.by_bus.length > 0 && (
        <table className="data-table">
          <thead><tr><th>Bus</th><th>Generation (MW)</th><th>Price</th></tr></thead>
          <tbody>
            {d.by_bus.map((b) => (
              <tr key={b.bus}><td>{b.bus}</td><td className="num">{fmtNumber(b.generation_mw)}</td><td className="num">{fmtNumber(b.price)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {d.flows.length > 0 && (
        <p className="muted">Flows: {d.flows.map((f) => `${f.name} ${fmtNumber(f.p0_mw)} MW`).join(', ')}</p>
      )}
    </div>
  );
}

function DashboardView({ run, bus, carrier, period, onData }: {
  run: string; bus: string | null; carrier: string | null; period: string | null; onData: (d: Dashboard) => void;
}) {
  const dash = useResource(() => stateApi.dashboard(run, { bus, carrier, period }), [run, bus, carrier, period]);
  const d = dash.data;
  useEffect(() => { if (d) onData(d); }, [d, onData]);
  const [showCurtail, setShowCurtail] = useState(false);

  const specs = useMemo(() => {
    if (!d) return null;
    const dispatch: ChartSpec = timeSpec(
      d, showCurtail ? 'dispatch+curtail' : 'dispatch',
      showCurtail ? 'Dispatch by carrier, with curtailment' : 'Dispatch by carrier', 'MW',
      showCurtail ? withCurtailment(d.dispatch_mw.series, d.curtailment_mw.series) : d.dispatch_mw.series,
      { kind: 'area', stacked: true },
    );
    if (d.dispatch_mw.load_mw) {
      dispatch.series = [...dispatch.series, { name: 'load', kind: 'line', color: '#6b7280', values: d.dispatch_mw.load_mw }];
    }
    const price = timeSpec(d, 'price', d.selected.bus ? `Price at ${d.selected.bus}` : 'Prices by bus', `${d.price.currency}/MWh`, d.price.series);
    const curtail = d.curtailment_mw.series.length
      ? timeSpec(d, 'curtail', 'Curtailment by carrier', 'MW', d.curtailment_mw.series, { kind: 'area', stacked: true }) : null;
    const soc = d.storage.soc_mwh.length
      ? timeSpec(d, 'soc', 'Storage state of charge', 'MWh', d.storage.soc_mwh) : null;
    const flow = d.storage.flow_mw
      ? timeSpec(d, 'storage-flow', 'Storage charge and discharge', 'MW', [
        { name: 'charge', values: d.storage.flow_mw.charge }, { name: 'discharge', values: d.storage.flow_mw.discharge }]) : null;
    const emissions = d.emissions_tco2e.values.some((v) => v !== null)
      ? timeSpec(d, 'emissions', 'System emissions', 'tCO2e per snapshot', [{ name: 'emissions', values: d.emissions_tco2e.values }], { kind: 'area' }) : null;
    const capacity = d.capacity_mw.categories.length ? categorySpec(d.run, `cap:${d.selected.bus}:${d.selected.carrier}`, 'Capacity: installed and optimal', 'MW', d.capacity_mw) : null;
    const energy = d.carrier_mix.length ? categorySpec(d.run, `mix:${d.selected.carrier}`, 'Energy by carrier', 'MWh', {
      categories: d.carrier_mix.map((m) => m.name),
      series: [{ name: 'energy', values: d.carrier_mix.map((m) => m.energy_mwh) }],
    }) : null;
    const emByCarrier = d.emissions_by_carrier.categories.length
      ? categorySpec(d.run, `em:${d.selected.bus}:${d.selected.carrier}`, 'Emissions by carrier', 'tCO2e', {
        categories: d.emissions_by_carrier.categories, series: [d.emissions_by_carrier.series[0]],
      }) : null;
    const cost = d.cost_breakdown.length ? categorySpec(d.run, 'cost', 'Cost breakdown', d.price.currency, {
      categories: d.cost_breakdown.map((c) => c.label),
      series: [{ name: 'cost', values: d.cost_breakdown.map((c) => c.value) }],
    }) : null;
    const loading = d.line_loading.length ? categorySpec(d.run, 'loading', 'Line loading', '% of rating', {
      categories: d.line_loading.map((l) => l.name), series: [{ name: 'loading', values: d.line_loading.map((l) => l.loading_pct) }],
    }) : null;
    const heat = d.price.series[0]
      ? dayHourHeatmap(d.x, d.price.series[0].values, `${d.run}:heat:${d.selected.bus}:${d.selected.period}`, 'Price by day and hour', `${d.price.currency}/MWh`)
      : null;
    return { dispatch, price, curtail, soc, flow, emissions, capacity, energy, emByCarrier, cost, loading, heat };
  }, [d, showCurtail]);

  if (dash.error && !d) return <p className="panel-error" role="alert">{dash.error}</p>;
  if (!d || !specs) return <p className="panel-empty muted">Loading the results.</p>;
  const extra = (t: DrillTarget) => (
    <SnapshotBreakdown run={d.run} index={d.snapshot_index[t.sourceIndex] ?? t.sourceIndex} bus={d.selected.bus} />
  );
  const time = (spec: ChartSpec | null, height = 260) => spec && (
    <Chart key={spec.chartId} spec={spec} wide height={height} drillExtra={extra} />
  );
  const plain = (spec: ChartSpec | null, height = 240) => spec && <Chart key={spec.chartId} spec={spec} wide height={height} />;

  return (
    <>
      <Kpis items={d.kpis} />
      {d.bus_filter_note && <p className="muted">{d.bus_filter_note}</p>}
      {d.stride > 1 && <p className="muted">Ragnarok stored every {d.stride}th snapshot of this run; the time charts show those.</p>}
      {d.curtailment_mw.series.length > 0 && (
        <label className="run-compare" title="Stack each carrier's curtailment on top of its dispatch: the top of the stack is what was available">
          <input type="checkbox" checked={showCurtail} onChange={(e) => setShowCurtail(e.target.checked)} />
          <span className="muted">Show curtailment on the dispatch chart</span>
        </label>
      )}
      {time(specs.dispatch, 300)}
      {time(specs.price)}
      {plain(specs.heat, specs.heat ? Math.min(640, Math.max(280, specs.heat.series.length * 5 + 110)) : 280)}
      <div className="chart-pair">{plain(specs.capacity)}{plain(specs.energy)}</div>
      {time(specs.flow)}
      {time(specs.soc)}
      {time(specs.curtail)}
      {time(specs.emissions)}
      <div className="chart-pair">{plain(specs.emByCarrier)}{plain(specs.cost)}</div>
      {plain(specs.loading)}
      {d.expansion.length > 0 && (
        <>
          <h3 className="panel-h">Capacity expansion</h3>
          <table className="data-table">
            <thead><tr><th>Asset</th><th>Carrier</th><th>Bus</th><th>Existing MW</th><th>Optimal MW</th><th>Added MW</th></tr></thead>
            <tbody>
              {d.expansion.map((e) => (
                <tr key={e.name}><td>{e.name}</td><td>{e.carrier}</td><td>{e.bus}</td>
                  <td className="num">{fmtNumber(e.p_nom_mw)}</td><td className="num">{fmtNumber(e.p_nom_opt_mw)}</td><td className="num">{fmtNumber(e.delta_mw)}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

function CompareView({ names }: { names: string[] }) {
  const cmp = useResource<Comparison>(() => stateApi.compare(names), [names.join('|')]);
  const c = cmp.data;
  const specs = useMemo(() => {
    if (!c) return null;
    const id = names.join('+');
    const baseX = c.price[0]?.x ?? [];
    return {
      gen: categorySpec(id, 'gen', 'Energy by carrier', 'MWh', c.generation_mwh),
      cap: categorySpec(id, 'cap', 'Optimal capacity by carrier', 'MW', c.capacity_mw),
      cost: categorySpec(id, 'cost', 'Cost breakdown', '', c.cost),
      price: {
        chartId: `${id}:price`, title: 'System price', unit: '$/MWh', x: baseX,
        series: c.price.map((p) => ({ name: p.name, values: p.values })),
      } as ChartSpec,
    };
  }, [c, names]);
  if (cmp.error && !c) return <p className="panel-error" role="alert">{cmp.error}</p>;
  if (!c || !specs) return <p className="panel-empty muted">Loading the comparison.</p>;
  return (
    <>
      <p className="muted">Deltas are against {c.runs[0].name}.</p>
      <div className="table-scroll">
        <table className="data-table compare-table">
          <thead>
            <tr><th>Metric</th>{c.runs.map((r, i) => <th key={r.name} title={r.name}>{i === 0 ? 'Baseline' : `Run ${i + 1}`}<br /><span className="muted">{r.name.slice(5, 16)}</span></th>)}</tr>
          </thead>
          <tbody>
            {c.kpis.map((k) => (
              <tr key={k.key}>
                <td>{k.label}{k.unit && k.key !== 'totalCost' ? <span className="muted"> {k.unit}</span> : ''}</td>
                {k.values.map((v, i) => (
                  <td key={i} className="num">
                    {v !== null ? fmtNumber(v, Math.abs(v) >= 100 ? 0 : 2) : (k.text[i] ?? '')}
                    {i > 0 && k.delta_pct[i] !== null && Math.abs(k.delta_pct[i] as number) >= 0.05 && (
                      <span className={`delta ${(k.delta_pct[i] as number) > 0 ? 'up' : 'down'}`}>
                        {(k.delta_pct[i] as number) > 0 ? ' +' : ' '}{fmtNumber(k.delta_pct[i], 1)}%
                      </span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Chart spec={specs.gen} wide height={240} />
      <div className="chart-pair"><Chart spec={specs.cap} wide height={240} /><Chart spec={specs.cost} wide height={240} /></div>
      <Chart spec={specs.price} wide height={260} />
    </>
  );
}

export function ResultsSection({ runs, selected, onSelect, compare, onCompare }: Props) {
  const [bus, setBus] = useState<string | null>(null);
  const [carrier, setCarrier] = useState<string | null>(null);
  const [period, setPeriod] = useState<string | null>(null);
  const [meta, setMeta] = useState<Dashboard | null>(null);
  const list = runs.data?.items ?? [];
  useEffect(() => { setBus(null); setCarrier(null); setPeriod(null); setMeta(null); }, [selected]);

  if (list.length === 0) {
    return <div className="panel-empty"><p><strong>No results yet.</strong></p><p className="muted">Solve the model, from the Runs tab or by asking the agent. The result opens here when it finishes.</p></div>;
  }
  const comparing = compare.length >= 2;
  const run = selected && list.some((r) => r.name === selected) ? selected : list[0].name;

  return (
    <div className="results-section">
      <div className="filter-bar">
        <label className="inline-select"><span className="muted">Run</span>
          <select value={run} onChange={(e) => onSelect(e.target.value)} aria-label="Run">
            {list.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
          </select>
        </label>
        <label className="inline-select"><span className="muted">Compare with</span>
          <select value={comparing ? compare.find((n) => n !== run) ?? '' : ''} aria-label="Compare with"
            onChange={(e) => onCompare(e.target.value ? [run, e.target.value] : [])}>
            <option value="">Nothing</option>
            {list.filter((r) => r.name !== run).map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
          </select>
        </label>
        {!comparing && (
          <>
            <label className="inline-select"><span className="muted">Bus</span>
              <select value={bus ?? ''} onChange={(e) => setBus(e.target.value || null)} aria-label="Bus">
                <option value="">All buses</option>
                {(meta?.buses ?? []).map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </label>
            <label className="inline-select"><span className="muted">Carrier</span>
              <select value={carrier ?? ''} onChange={(e) => setCarrier(e.target.value || null)} aria-label="Carrier">
                <option value="">All carriers</option>
                {(meta?.carriers ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            {(meta?.periods.length ?? 0) > 0 && (
              <label className="inline-select"><span className="muted">Period</span>
                <select value={period ?? ''} onChange={(e) => setPeriod(e.target.value || null)} aria-label="Period">
                  <option value="">All periods</option>
                  {meta!.periods.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </label>
            )}
            {(bus || carrier || period) && <button className="btn-ghost sm" onClick={() => { setBus(null); setCarrier(null); setPeriod(null); }}>Clear</button>}
          </>
        )}
      </div>
      {comparing
        ? <CompareView names={[run, ...compare.filter((n) => n !== run)]} />
        : <DashboardView run={run} bus={bus} carrier={carrier} period={period}
            onData={(d) => setMeta((m) => (m && m.run === d.run && m.buses.join() === d.buses.join()
              && m.carriers.join() === d.carriers.join() && m.periods.join() === d.periods.join() ? m : d))} />}
    </div>
  );
}
