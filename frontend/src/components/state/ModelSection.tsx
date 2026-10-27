/**
 * Model: what is in the network right now.
 *
 * Counts by component type (click one for its rows), carriers, and the topology as a
 * map. Click a bus to see what hangs off it; click an asset to jump to its row in
 * the table. Everything is read from `/api/state/model`, so component types and
 * carriers are whatever the model holds — nothing here knows what a "generator" is.
 */
import { useMemo, useState } from 'react';
import { StateModel } from '../../api';
import { paletteColour } from '../../lib/chartModel';
import { fmtNumber } from '../../lib/format';
import { Resource } from '../../lib/useResource';
import { TableView } from './TableView';
import { TopologyMap } from './TopologyMap';

interface Props {
  model: Resource<StateModel>;
  engineVersion: number | null;
}

export function ModelSection({ model, engineVersion }: Props) {
  const [sheet, setSheet] = useState<{ name: string; q: string } | null>(null);
  const [bus, setBus] = useState<string | null>(null);
  const [branch, setBranch] = useState<string | null>(null);
  const data = model.data;

  const colourOf = useMemo(() => {
    const names = (data?.carriers ?? []).map((c) => c.name).sort();
    const own = new Map((data?.carriers ?? []).filter((c) => c.color).map((c) => [c.name, c.color]));
    return (carrier: string) => own.get(carrier) ?? (carrier ? paletteColour(Math.max(0, names.indexOf(carrier))) : 'var(--muted)');
  }, [data?.carriers]);

  if (sheet) {
    return <TableView sheet={sheet.name} initialQuery={sheet.q} engineVersion={engineVersion} onClose={() => setSheet(null)} />;
  }
  if (model.error && !data) return <p className="panel-error" role="alert">{model.error}</p>;
  if (!data) return <p className="panel-empty muted">Loading the model.</p>;
  if (!data.loaded) {
    return (
      <div className="panel-empty">
        <p><strong>No model loaded.</strong></p>
        <p className="muted">Ask the agent to build one, or open a project in the Ragnarok tab. It will appear here as it is built.</p>
      </div>
    );
  }

  const selectedBus = data.topology.buses.find((b) => b.name === bus) ?? null;
  const selectedBranch = data.topology.branches.find((b) => b.name === branch) ?? null;
  const components = data.counts.filter((c) => c.role !== 'other' || c.sheet !== 'carriers');
  const maxCount = Math.max(1, ...components.map((c) => c.count));

  return (
    <div className="model-section">
      <div className="panel-summary">
        <strong>{data.filename || 'Untitled model'}</strong>
        <span className="muted">
          {data.snapshot_count ? `${data.snapshot_count.toLocaleString()} snapshots` : 'no snapshots'}
          {data.snapshot_start ? `, ${data.snapshot_start.slice(0, 16)} to ${(data.snapshot_end ?? '').slice(0, 16)}` : ''}
        </span>
      </div>

      <h3 className="panel-h">Components</h3>
      <div className="count-grid">
        {components.map((c) => (
          <button key={c.sheet} className="count-card" onClick={() => setSheet({ name: c.sheet, q: '' })}
            title={`Open the ${c.label} table`}>
            <span className="count-num">{c.count.toLocaleString()}</span>
            <span className="count-label">{c.label}</span>
            <span className="count-bar"><i style={{ width: `${(c.count / maxCount) * 100}%` }} /></span>
          </button>
        ))}
      </div>
      {(data.series_sheets?.length ?? 0) > 0 && (
        <p className="series-links muted">
          Time series:{' '}
          {data.series_sheets!.map((s) => (
            <button key={s.name} className="link-btn" onClick={() => setSheet({ name: s.name, q: '' })}>
              {s.name} ({s.rows?.toLocaleString() ?? '?'} rows)
            </button>
          ))}
        </p>
      )}

      {data.topology.buses.length > 0 && (
        <>
          <h3 className="panel-h">Network</h3>
          <TopologyMap buses={data.topology.buses} branches={data.topology.branches}
            hasCoordinates={data.topology.has_coordinates} colourOf={colourOf}
            selectedBus={bus} selectedBranch={branch} onSelectBus={setBus} onSelectBranch={setBranch} />
          {selectedBus && (
            <div className="detail-card">
              <div className="detail-head">
                <strong>{selectedBus.name}</strong>
                <span className="muted">
                  {selectedBus.v_nom_kv !== null ? `${fmtNumber(selectedBus.v_nom_kv)} kV, ` : ''}
                  {selectedBus.assets_total} attached asset{selectedBus.assets_total === 1 ? '' : 's'}
                </span>
                <button className="btn-ghost sm" onClick={() => setBus(null)}>Clear</button>
              </div>
              <ul className="asset-list">
                {selectedBus.assets.map((a) => (
                  <li key={`${a.sheet}:${a.name}`}>
                    <button className="asset-row" onClick={() => setSheet({ name: a.sheet, q: a.name })}
                      title={`Open ${a.name} in the ${a.sheet} table`}>
                      <i className="swatch" style={{ background: colourOf(a.carrier) }} />
                      <span className="asset-name">{a.name}</span>
                      <span className="muted">{a.sheet.replace(/_/g, ' ')}{a.carrier ? `, ${a.carrier}` : ''}</span>
                      <span className="asset-nom">{a.p_nom_mw !== null ? `${fmtNumber(a.p_nom_mw)} MW` : ''}</span>
                    </button>
                  </li>
                ))}
                {selectedBus.assets_total > selectedBus.assets.length && (
                  <li className="muted">and {selectedBus.assets_total - selectedBus.assets.length} more (see the tables)</li>
                )}
              </ul>
            </div>
          )}
          {selectedBranch && (
            <div className="detail-card">
              <div className="detail-head">
                <strong>{selectedBranch.name}</strong>
                <span className="muted">
                  {selectedBranch.sheet}: {selectedBranch.bus0} to {selectedBranch.bus1}
                  {selectedBranch.capacity_mw !== null ? `, ${fmtNumber(selectedBranch.capacity_mw)} MW` : ''}
                </span>
                <button className="btn-ghost sm" onClick={() => setSheet({ name: selectedBranch.sheet, q: selectedBranch.name })}>Open row</button>
              </div>
            </div>
          )}
          {data.topology.unattached > 0 && (
            <p className="muted">{data.topology.unattached} asset{data.topology.unattached === 1 ? ' is' : 's are'} attached to a bus that does not exist.</p>
          )}
        </>
      )}

      {data.carriers.length > 0 && (
        <>
          <h3 className="panel-h">Carriers</h3>
          <ul className="carrier-list">
            {data.carriers.map((c) => (
              <li key={c.name}>
                <i className="swatch" style={{ background: colourOf(c.name) }} />
                <span className="carrier-name">{c.nice_name || c.name}</span>
                <span className="muted">{c.asset_count} asset{c.asset_count === 1 ? '' : 's'}</span>
                <span className="muted carrier-co2">
                  {c.co2_emissions_t_per_mwh_th ? `${fmtNumber(c.co2_emissions_t_per_mwh_th)} tCO2/MWh th` : ''}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
