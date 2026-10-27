/**
 * One sheet of the model as a sortable, filterable, paged table.
 *
 * Sorting, filtering and paging happen in Bifrost's `/api/state/sheet/{name}` (Ragnarok
 * pages but does not sort), so the browser holds one page, not the sheet. It refetches
 * when the model changes, so rows the agent edits update under the reader.
 */
import { useEffect, useState } from 'react';
import { CellValue, stateApi } from '../../api';
import { fmtNumber } from '../../lib/format';
import { useResource } from '../../lib/useResource';

const PAGE = 50;

function cell(value: CellValue): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return fmtNumber(value, Math.abs(value) < 1000 ? 4 : 0);
  return String(value);
}

interface Props {
  sheet: string;
  engineVersion: number | null;
  initialQuery?: string;
  onClose: () => void;
}

export function TableView({ sheet, engineVersion, initialQuery = '', onClose }: Props) {
  const [q, setQ] = useState(initialQuery);
  const [sort, setSort] = useState<string | null>(null);
  const [desc, setDesc] = useState(false);
  const [offset, setOffset] = useState(0);
  const [allColumns, setAllColumns] = useState(false);

  useEffect(() => { setQ(initialQuery); setOffset(0); setSort(null); }, [sheet, initialQuery]);

  const page = useResource(
    () => stateApi.sheet(sheet, { q, sort: sort ?? undefined, desc, offset, limit: PAGE }),
    [sheet, q, sort, desc, offset, engineVersion], true, q ? 250 : 0,
  );
  const data = page.data;
  const columns = data ? (allColumns || data.key_columns.length < 2 ? data.columns : data.key_columns) : [];
  const from = data && data.total > 0 ? data.offset + 1 : 0;
  const to = data ? Math.min(data.offset + data.items.length, data.total) : 0;

  const header = (column: string) => {
    if (sort === column) setDesc((d) => !d);
    else { setSort(column); setDesc(false); }
    setOffset(0);
  };

  return (
    <div className="table-view">
      <div className="table-bar">
        <button className="btn-ghost sm" onClick={onClose}>Back</button>
        <strong className="table-title">{sheet.replace(/_/g, ' ')}</strong>
        <input className="table-filter" type="search" value={q} placeholder="Filter rows"
          aria-label={`Filter ${sheet}`} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        {data && data.key_columns.length >= 2 && data.key_columns.length < data.columns.length && (
          <button className={`btn-ghost sm${allColumns ? ' is-on' : ''}`} aria-pressed={allColumns}
            onClick={() => setAllColumns((v) => !v)}>
            {allColumns ? 'Varying columns' : `All ${data.columns.length} columns`}
          </button>
        )}
      </div>
      {page.error && <p className="panel-error" role="alert">{page.error}</p>}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c} scope="col" aria-sort={sort === c ? (desc ? 'descending' : 'ascending') : 'none'}>
                  <button onClick={() => header(c)} title={`Sort by ${c}`}>
                    {c}{sort === c && <span className="sort-mark">{desc ? ' desc' : ' asc'}</span>}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((row, i) => (
              <tr key={`${data?.offset}-${i}`}>
                {columns.map((c) => {
                  const v = row[c];
                  return <td key={c} className={typeof v === 'number' ? 'num' : undefined}>{cell(v)}</td>;
                })}
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr><td colSpan={Math.max(columns.length, 1)} className="muted">No rows{q ? ' match that filter' : ''}.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="table-foot muted">
        <span>{data ? `${from}-${to} of ${data.total.toLocaleString()} rows` : 'Loading'}</span>
        <span className="table-pager">
          <button className="btn-ghost sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <button className="btn-ghost sm" disabled={!data || offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </span>
      </div>
    </div>
  );
}
