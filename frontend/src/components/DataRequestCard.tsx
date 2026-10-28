/**
 * The agent asking the user for rows it cannot get from open data.
 *
 * Three ways in, one path out: paste CSV (or a spreadsheet selection — tab
 * separated works), drop / choose a file, or fill a small grid. All three become
 * text, which the BACKEND validates against Ragnarok's live schema and the model
 * (columns, types, units, bus names). The card only renders what it is told: it
 * does not re-implement a single rule, so a rule changed in one place cannot
 * disagree with the card.
 *
 * Errors are row-level (line of the pasted text, column, message). Submitting
 * rows with errors merges nothing and keeps the card open. A clean submit merges
 * through Ragnarok — the user's own mutation, so there is no approval step — and
 * the card settles into a summary. Cancel tells the agent not to invent the rows.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, ChatEvent, DataColumn, DataReport } from '../api';

interface Props {
  event: ChatEvent;
  /** The agent's outcome text once resolved, or null while it is still waiting. */
  outcome: string | null;
  /** Open the merge's journal entries in the State panel. */
  onOpenJournal: (ids: number[]) => void;
}

const csvCell = (v: string) => (/[",\n\t]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);

/** The grid's rows as CSV, skipping rows the user left entirely blank. */
function gridToCsv(cols: string[], rows: string[][]): string {
  const filled = rows.filter((r) => r.some((c) => c.trim()));
  if (filled.length === 0) return '';
  return [cols.map(csvCell).join(','), ...filled.map((r) => r.map((c) => csvCell(c.trim())).join(','))].join('\n');
}

export function DataRequestCard({ event, outcome, onOpenJournal }: Props) {
  const id = event.requestId ?? '';
  const columns: DataColumn[] = event.columns ?? [];
  const needed = event.needed ?? [];
  // The grid starts with the columns the agent needs; the rest can be added.
  const [gridCols, setGridCols] = useState<string[]>(needed);
  const [grid, setGrid] = useState<string[][]>([needed.map(() => '')]);
  const [mode, setMode] = useState<'paste' | 'form'>('paste');
  const [text, setText] = useState('');
  const [report, setReport] = useState<DataReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [merged, setMerged] = useState<DataReport | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const lastChecked = useRef('');

  const csv = mode === 'paste' ? text : gridToCsv(gridCols, grid);
  const addable = useMemo(() => columns.filter((c) => !gridCols.includes(c.name)), [columns, gridCols]);

  // Validate as the user types (debounced): the errors ARE the feedback, so they
  // should not wait for a button. Nothing is changed by validating.
  useEffect(() => {
    if (outcome !== null || !csv.trim() || csv === lastChecked.current) return undefined;
    const timer = window.setTimeout(() => {
      lastChecked.current = csv;
      api.validateData(id, csv).then(setReport).catch((e) => setFailed(e instanceof Error ? e.message : 'Could not validate.'));
    }, 450);
    return () => window.clearTimeout(timer);
  }, [csv, id, outcome]);

  const load = async (file: File | undefined) => {
    if (!file) return;
    if (/\.(xlsx?|zip)$/i.test(file.name)) {
      setFailed(`${file.name} is a binary workbook — export the sheet as CSV and drop that.`);
      return;
    }
    setFailed(null);
    setMode('paste');
    setText(await file.text());
  };

  const submit = async () => {
    setBusy(true);
    setFailed(null);
    try {
      const result = await api.submitData(id, csv);
      setReport(result);
      lastChecked.current = csv;
      if (result.merged) setMerged(result);
    } catch (e) {
      setFailed(e instanceof Error ? e.message : 'Could not merge those rows.');
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try { await api.cancelData(id); } catch (e) { setFailed(e instanceof Error ? e.message : 'Could not cancel.'); }
    setBusy(false);
  };

  if (outcome !== null) {
    let done: { status?: string; rowsMerged?: number; sheet?: string; journalEntryIds?: number[] } | null = null;
    try { done = JSON.parse(outcome); } catch { /* cancelled / timed out: plain text */ }
    const ids = done?.journalEntryIds ?? merged?.summary?.journalEntryIds ?? [];
    return (
      <div className="card card--settled data-card" data-testid="data-card-settled">
        <p className="card-q">{event.reason || `Data for ${event.sheet}`}</p>
        {done?.status === 'merged' ? (
          <p className="card-a">
            You supplied <strong>{done.rowsMerged} row{done.rowsMerged === 1 ? '' : 's'}</strong> for{' '}
            <code>{done.sheet}</code>; Ragnarok merged them.{' '}
            {ids.length > 0 && <button className="link-btn" onClick={() => onOpenJournal(ids)}>
              View the change{ids.length > 1 ? 's' : ''}
            </button>}
          </p>
        ) : (
          <p className="card-a">{outcome.startsWith('The user CANCELLED') ? 'You cancelled — the agent was told not to invent these rows.' : 'No data was supplied.'}</p>
        )}
      </div>
    );
  }

  return (
    <div className="card data-card" data-testid="data-card">
      <p className="card-q">The agent needs your data for <code>{event.sheet}</code></p>
      {event.reason && <p className="card-a">{event.reason}</p>}
      <details className="data-cols">
        <summary>{columns.length} columns — required: {needed.join(', ')}</summary>
        <table>
          <thead><tr><th>column</th><th>type</th><th>unit</th><th /></tr></thead>
          <tbody>
            {columns.map((c) => (
              <tr key={c.name} className={needed.includes(c.name) ? 'is-required' : ''} title={c.description}>
                <td>{c.name}{needed.includes(c.name) ? ' *' : ''}</td><td>{c.type}</td><td>{c.unit}</td>
                <td className="muted">{c.default && c.default !== 'n/a' ? `default ${c.default}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      {event.example && <pre className="data-example" title="Example the agent suggested">{event.example}</pre>}

      <div className="data-tabs" role="tablist">
        <button role="tab" aria-selected={mode === 'paste'} className={mode === 'paste' ? 'is-on' : ''} onClick={() => setMode('paste')}>Paste CSV</button>
        <button role="tab" aria-selected={mode === 'form'} className={mode === 'form' ? 'is-on' : ''} onClick={() => setMode('form')}>Form</button>
        <span className="data-spacer" />
        <input ref={picker} type="file" accept=".csv,.tsv,.txt,text/csv" hidden
          onChange={(e) => { void load(e.target.files?.[0]); e.target.value = ''; }} />
        <button className="btn-ghost" onClick={() => picker.current?.click()}>Choose a file…</button>
      </div>

      {mode === 'paste' ? (
        <textarea
          className="data-paste" rows={6} value={text} spellCheck={false}
          placeholder={`Paste CSV here, or drop a .csv file on this box.\nFirst line = column names, e.g. ${needed.join(',')}\nA unit may follow a name: p_nom [kW]`}
          onChange={(e) => setText(e.target.value)}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); e.stopPropagation(); void load(e.dataTransfer.files?.[0]); }}
        />
      ) : (
        <div className="data-grid-wrap">
          <table className="data-grid">
            <thead><tr>{gridCols.map((c) => <th key={c}>{c}<em>{columns.find((x) => x.name === c)?.unit?.replace('n/a', '')}</em></th>)}</tr></thead>
            <tbody>
              {grid.map((row, i) => (
                <tr key={i}>
                  {gridCols.map((c, j) => (
                    <td key={c}>
                      <input value={row[j] ?? ''} aria-label={`${c} row ${i + 1}`}
                        onChange={(e) => setGrid((g) => g.map((r, k) => (k === i ? r.map((v, m) => (m === j ? e.target.value : v)) : r)))} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="data-grid-tools">
            <button className="btn-ghost" onClick={() => setGrid((g) => [...g, gridCols.map(() => '')])}>+ row</button>
            {addable.length > 0 && (
              <select value="" aria-label="add a column" onChange={(e) => {
                const name = e.target.value;
                if (!name) return;
                setGridCols((cols) => [...cols, name]);
                setGrid((g) => g.map((r) => [...r, '']));
              }}>
                <option value="">+ column…</option>
                {addable.map((c) => <option key={c.name} value={c.name}>{c.name}{c.unit !== 'n/a' ? ` (${c.unit})` : ''}</option>)}
              </select>
            )}
          </div>
        </div>
      )}

      {report && (
        <div className="data-report" data-testid="data-report">
          {report.errors.length > 0 ? (
            <ul className="data-errors">
              {report.errors.slice(0, 30).map((er, i) => (
                <li key={i}><span className="data-line">{er.line ? `line ${er.line}` : 'file'}</span>{er.message}</li>
              ))}
              {report.errors.length > 30 && <li className="muted">…and {report.errors.length - 30} more</li>}
            </ul>
          ) : (
            <p className="data-ok">{report.rowCount} row{report.rowCount === 1 ? '' : 's'} valid for <code>{report.sheet}</code>.</p>
          )}
          {report.warnings.length > 0 && (
            <ul className="data-warnings">
              {report.warnings.slice(0, 10).map((w, i) => (
                <li key={i}><span className="data-line">{w.line ? `line ${w.line}` : ''}</span>{w.message}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {failed && <span className="card-fail">{failed}</span>}

      <div className="data-actions">
        <button className="btn-primary" disabled={busy || !csv.trim() || (report !== null && lastChecked.current === csv && !report.ok)}
          onClick={() => void submit()}>
          {busy ? 'Working…' : 'Add rows to the model'}
        </button>
        <button className="btn-ghost" disabled={busy} onClick={() => void cancel()}>Cancel</button>
        <span className="muted">Checked against Ragnarok's schema; merged only when every row is valid.</span>
      </div>
    </div>
  );
}
