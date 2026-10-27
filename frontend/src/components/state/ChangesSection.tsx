/**
 * Changes: Ragnarok's journal as a timeline.
 *
 * Bifrost keeps no journal of its own — Ragnarok records every mutation at its store
 * facade, whoever made it. Each entry says who (agent or user), what kind of change,
 * which sheets it touched and by how many rows; expanding one fetches its cell-level
 * diff. An entry made by an agent tool links to the chat step that produced it (the
 * `tool.end` event carries the journal ids) and the chat links back here.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { JournalDiff, JournalEntry, stateApi } from '../../api';
import { fmtNumber } from '../../lib/format';
import { Resource } from '../../lib/useResource';

interface Props {
  journal: Resource<import('../../api').JournalPage>;
  /** Entry ids to highlight and scroll to (set by a chat step's change chip). */
  focusIds: number[];
  focusNonce: number;
  /** Which chat step (tool call id) produced a journal entry, if any did. */
  callOf: ReadonlyMap<number, string>;
  onFocusStep: (callId: string) => void;
}

function clock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function rowDelta(before: number | null, after: number | null): string {
  if (before === null) return 'new';
  if (after === null) return 'removed';
  const d = after - before;
  return d === 0 ? 'edited' : `${d > 0 ? '+' : ''}${d} rows`;
}

/** Sheets whose row count moved, then one line for the rest (a model replace rewrites every sheet). */
function sheetChips(sheets: JournalEntry['sheets']) {
  const moved = sheets.filter((s) => s.added || s.rowsBefore !== s.rowsAfter);
  const rest = sheets.length - moved.length;
  return (
    <>
      {moved.slice(0, 8).map((s) => (
        <span key={s.name} className="chip chip--muted">{s.name} {rowDelta(s.rowsBefore, s.rowsAfter)}</span>
      ))}
      {moved.length > 8 && <span className="muted note-sm">and {moved.length - 8} more</span>}
      {rest > 0 && <span className="muted note-sm">{moved.length ? 'and ' : ''}{rest} sheet{rest === 1 ? '' : 's'} rewritten, same row counts</span>}
    </>
  );
}

function DiffView({ id }: { id: number }) {
  const [diff, setDiff] = useState<JournalDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    stateApi.journalDiff(id).then((d) => { if (live) setDiff(d); }, (e: unknown) => { if (live) setError(e instanceof Error ? e.message : 'Could not load the diff.'); });
    return () => { live = false; };
  }, [id]);
  if (error) return <p className="panel-error">{error}</p>;
  if (!diff) return <p className="muted">Loading the diff.</p>;
  const cells = diff.detail.cellDiffs ?? [];
  const other = Object.entries(diff.detail).filter(([k]) => k !== 'cellDiffs' && k !== 'truncated');
  return (
    <div className="diff">
      {cells.length > 0 && (
        <table className="data-table diff-table">
          <thead><tr><th>Row</th><th>Column</th><th>Before</th><th>After</th></tr></thead>
          <tbody>
            {cells.map((c, i) => (
              <tr key={i}>
                <td className="num">{c.row}</td><td>{c.column}</td>
                <td className="diff-before">{typeof c.before === 'number' ? fmtNumber(c.before, 4) : String(c.before ?? '')}</td>
                <td className="diff-after">{typeof c.after === 'number' ? fmtNumber(c.after, 4) : String(c.after ?? '')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {diff.detail.truncated && <p className="muted">The diff is capped; more cells changed than are shown.</p>}
      {other.map(([k, v]) => (
        <details key={k}><summary>{k}</summary><pre className="diff-raw">{JSON.stringify(v, null, 2).slice(0, 4000)}</pre></details>
      ))}
      {cells.length === 0 && other.length === 0 && <p className="muted">No cell-level detail was kept for this change.</p>}
    </div>
  );
}

export function ChangesSection({ journal, focusIds, focusNonce, callOf, onFocusStep }: Props) {
  const [actor, setActor] = useState('all');
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [older, setOlder] = useState<JournalEntry[]>([]);
  const [olderEnd, setOlderEnd] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLOListElement>(null);

  const items = journal.data?.items;
  const entries = useMemo(() => {
    const first = items ?? [];
    const floor = first.length ? first[first.length - 1].id : Infinity;
    return [...first, ...older.filter((e) => e.id < floor)];
  }, [items, older]);
  const cursor = older.length ? older[older.length - 1].id : journal.data?.next_before ?? null;
  const actors = useMemo(() => ['all', ...Array.from(new Set(entries.map((e) => e.actor)))], [entries]);
  const shown = entries.filter((e) => actor === 'all' || e.actor === actor);

  // A chat step sent us here: open those entries and bring the first into view.
  useEffect(() => {
    if (!focusIds.length) return;
    setActor('all');
    setOpen((o) => new Set([...o, ...focusIds]));
    window.setTimeout(() => {
      listRef.current?.querySelector(`[data-entry-id="${focusIds[0]}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 60);
  }, [focusIds, focusNonce]);

  const loadOlder = async () => {
    if (cursor === null || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await stateApi.journal(100, cursor);
      setOlder((o) => [...o, ...page.items]);
      if (page.next_before === null) setOlderEnd(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load older changes.');
    } finally {
      setLoadingOlder(false);
    }
  };

  if (journal.error && !journal.data) return <p className="panel-error" role="alert">{journal.error}</p>;
  if (!journal.data) return <p className="panel-empty muted">Loading the timeline.</p>;
  if (entries.length === 0) {
    return <div className="panel-empty"><p><strong>No changes yet.</strong></p><p className="muted">Every edit to the model, by the agent or by you, is recorded here as it happens.</p></div>;
  }

  return (
    <div className="changes-section">
      <div className="panel-summary">
        <strong>{journal.data.version ?? entries.length} changes</strong>
        <label className="inline-select">
          <span className="muted">By</span>
          <select value={actor} onChange={(e) => setActor(e.target.value)} aria-label="Filter by who made the change">
            {actors.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
      </div>
      {(error || journal.error) && <p className="panel-error" role="alert">{error ?? journal.error}</p>}
      <ol className="timeline" ref={listRef}>
        {shown.map((e) => {
          const isOpen = open.has(e.id);
          const call = callOf.get(e.id);
          return (
            <li key={e.id} data-entry-id={e.id}
              className={`timeline-item actor-${e.actor}${focusIds.includes(e.id) ? ' is-focus' : ''}`}>
              <div className="timeline-head">
                <span className="timeline-time muted">{clock(e.ts)}</span>
                <span className={`chip chip--actor-${e.actor}`}>{e.actor}</span>
                <span className="chip chip--muted">{e.kind}</span>
                <span className="muted timeline-id">#{e.id}</span>
              </div>
              <p className="timeline-summary">{e.summary}</p>
              <div className="timeline-sheets">
                {sheetChips(e.sheets)}
              </div>
              <div className="timeline-actions">
                {e.hasDetail && (
                  <button className="btn-ghost sm" aria-expanded={isOpen}
                    onClick={() => setOpen((o) => { const n = new Set(o); if (n.has(e.id)) n.delete(e.id); else n.add(e.id); return n; })}>
                    {isOpen ? 'Hide what changed' : 'Show what changed'}
                  </button>
                )}
                {call && <button className="btn-ghost sm" onClick={() => onFocusStep(call)}>Show the chat step</button>}
                {!call && e.actor === 'agent' && <span className="muted">step not in this transcript</span>}
              </div>
              {isOpen && e.hasDetail && <DiffView id={e.id} />}
            </li>
          );
        })}
      </ol>
      {cursor !== null && !olderEnd && (
        <button className="btn-ghost" onClick={() => void loadOlder()} disabled={loadingOlder}>
          {loadingOlder ? 'Loading' : 'Load older changes'}
        </button>
      )}
    </div>
  );
}
