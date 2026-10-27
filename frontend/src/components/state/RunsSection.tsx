/**
 * Runs: the solve queue and the stored runs.
 *
 * Ragnarok reports a queued job's state (queued, running, done, error) and when it
 * started, but not how far through it is, so there is no percentage here: a running
 * job shows an indeterminate bar and a live elapsed timer rather than an invented
 * number. Pick a run to open its results; tick up to five to compare them.
 */
import { useState } from 'react';
import { QueueJob, QueueState, RunMeta, stateApi, ListResponse } from '../../api';
import { Resource, useNow } from '../../lib/useResource';

interface Props {
  queue: Resource<QueueState>;
  runs: Resource<ListResponse<RunMeta>>;
  selected: string | null;
  compare: string[];
  onSelect: (name: string) => void;
  onToggleCompare: (name: string) => void;
  onOpenCompare: () => void;
}

function seconds(total: number): string {
  if (total < 60) return `${total.toFixed(total < 10 ? 1 : 0)} s`;
  return `${Math.floor(total / 60)} min ${Math.round(total % 60)} s`;
}

function when(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function JobRow({ job, received, now, onOpen, onCancel }: {
  job: QueueJob; received: number; now: number; onOpen: (run: string) => void; onCancel: (id: string) => void;
}) {
  const running = job.status === 'running';
  // elapsed_s was true when the response was built; add the time since we received it.
  const elapsed = job.elapsed_s !== null ? job.elapsed_s + (running ? Math.max(0, (now - received) / 1000) : 0) : null;
  return (
    <li className={`job job--${job.status}`}>
      <div className="job-head">
        <span className={`chip chip--status-${job.status}`}>{job.status}</span>
        <span className="job-label">{job.label || 'Solve'}</span>
        <span className="muted job-meta">
          {when(job.submittedAt)}{elapsed !== null ? `, ${seconds(elapsed)}` : ''}{job.cores ? `, ${job.cores} cores` : ''}
        </span>
      </div>
      {running && <div className="progress progress--indeterminate" role="progressbar" aria-label="Solving" />}
      {job.status === 'queued' && <div className="progress progress--waiting" aria-hidden />}
      {job.error && (
        <details className="job-error">
          <summary className="panel-error">{job.error.length > 110 ? `${job.error.slice(0, 110)}...` : job.error}</summary>
          <p className="panel-error">{job.error}</p>
        </details>
      )}
      <div className="job-actions">
        {job.status === 'done' && job.runName && <button className="btn-ghost sm" onClick={() => onOpen(job.runName as string)}>Open results</button>}
        {(job.status === 'queued' || job.status === 'running' || job.status === 'staged') && (
          <button className="btn-ghost sm danger" onClick={() => onCancel(job.id)}>Cancel</button>
        )}
      </div>
    </li>
  );
}

export function RunsSection({ queue, runs, selected, compare, onSelect, onToggleCompare, onOpenCompare }: Props) {
  const [discount, setDiscount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = queue.data?.items ?? [];
  const running = items.some((j) => j.status === 'running');
  const now = useNow(running);
  // When this queue snapshot arrived: elapsed times tick forward from here.
  const [received, setReceived] = useState(Date.now());
  const [lastQueue, setLastQueue] = useState<QueueState | null>(null);
  if (queue.data !== lastQueue) { setLastQueue(queue.data); setReceived(Date.now()); }

  const solve = async () => {
    setBusy(true);
    setError(null);
    try {
      const rate = discount.trim() === '' ? null : Number(discount);
      if (rate !== null && !Number.isFinite(rate)) throw new Error('The discount rate must be a number, e.g. 0.05.');
      await stateApi.solve(rate === null ? {} : { discountRate: rate });
      queue.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not queue the solve.');
    } finally {
      setBusy(false);
    }
  };
  const cancel = (id: string) => {
    stateApi.cancel(id).then(() => queue.reload(), (e: unknown) => setError(e instanceof Error ? e.message : 'Could not cancel.'));
  };

  return (
    <div className="runs-section">
      <h3 className="panel-h">Queue</h3>
      <div className="solve-bar">
        <button className="btn-primary sm" onClick={() => void solve()} disabled={busy}>{busy ? 'Queueing' : 'Solve this model'}</button>
        <label className="inline-select">
          <span className="muted">Discount rate</span>
          <input className="rate-input" inputMode="decimal" value={discount} placeholder="optional, e.g. 0.05"
            onChange={(e) => setDiscount(e.target.value)} aria-label="Discount rate (needed when capacity is extendable)" />
        </label>
        {queue.data && <span className="muted">{queue.data.active} active{queue.data.concurrency ? `, ${queue.data.concurrency} at a time` : ''}</span>}
      </div>
      {error && <p className="panel-error" role="alert">{error}</p>}
      {queue.error && !queue.data && <p className="panel-error" role="alert">{queue.error}</p>}
      {items.length === 0
        ? <p className="muted">Nothing queued. The agent, or the button above, queues a solve of the current model.</p>
        : (
          <ul className="job-list">
            {items.slice().reverse().slice(0, 12).map((job) => (
              <JobRow key={job.id} job={job} received={received} now={now} onOpen={onSelect} onCancel={cancel} />
            ))}
          </ul>
        )}

      <h3 className="panel-h">
        Stored runs
        {compare.length >= 2 && <button className="btn-ghost sm" onClick={onOpenCompare}>Compare {compare.length}</button>}
      </h3>
      {runs.error && !runs.data && <p className="panel-error" role="alert">{runs.error}</p>}
      {runs.data && runs.data.items.length === 0 && <p className="muted">No runs yet. Solve the model to create one.</p>}
      <ul className="run-list">
        {(runs.data?.items ?? []).map((r) => (
          <li key={r.name} className={`run-card${selected === r.name ? ' is-selected' : ''}`}>
            <button className="run-main" onClick={() => onSelect(r.name)} aria-pressed={selected === r.name}>
              <span className="run-name">{r.name}</span>
              <span className="muted">{r.label}{r.origin ? `, ${r.origin}` : ''}{r.saved_at ? `, ${when(r.saved_at)}` : ''}</span>
              <span className="run-kpis">
                {r.summary.slice(0, 3).map((k) => <span key={k.label} className="chip chip--muted">{k.label}: {k.value}</span>)}
              </span>
            </button>
            <label className="run-compare" title="Tick two to five runs, then Compare">
              <input type="checkbox" checked={compare.includes(r.name)} onChange={() => onToggleCompare(r.name)} />
              <span className="muted">Compare</span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}
