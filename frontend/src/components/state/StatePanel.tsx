/**
 * The State panel: a live view of the model and its runs beside the conversation.
 *
 * Five sections — Model, Changes, Runs, Results, Pinned — each driven by Bifrost's
 * `/api/state/*` routes and refreshed as the agent works (see lib/useStateData.ts).
 * The data hooks live above this component so switching section never refetches.
 */
import { useEffect, useRef, useState } from 'react';
import { Chart, ChartSpec } from '../Chart';
import { StateData } from '../../lib/useStateData';
import { ChangesSection } from './ChangesSection';
import { ModelSection } from './ModelSection';
import { ResultsSection } from './ResultsSection';
import { RunsSection } from './RunsSection';

export type StateTab = 'model' | 'changes' | 'runs' | 'results' | 'pinned';

export interface StateCommand {
  tab: StateTab;
  /** Journal entries to highlight on the Changes tab. */
  journalIds?: number[];
  nonce: number;
}

export interface PinnedChart {
  id: string;
  spec: ChartSpec;
}

interface Props {
  data: StateData;
  engineVersion: number | null;
  command: StateCommand | null;
  callOf: ReadonlyMap<number, string>;
  onFocusStep: (callId: string) => void;
  pinned: PinnedChart[];
  onUnpin: (id: string) => void;
  onCollapse: () => void;
}

const TABS: { id: StateTab; label: string }[] = [
  { id: 'model', label: 'Model' },
  { id: 'changes', label: 'Changes' },
  { id: 'runs', label: 'Runs' },
  { id: 'results', label: 'Results' },
  { id: 'pinned', label: 'Pinned' },
];

export function StatePanel(props: Props) {
  const { data, engineVersion, command, pinned } = props;
  const [tab, setTab] = useState<StateTab>('model');
  const [run, setRun] = useState<string | null>(null);
  const [compare, setCompare] = useState<string[]>([]);
  const [newRun, setNewRun] = useState(false);
  const seenDone = useRef<Set<string> | null>(null);

  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (command) setTab(command.tab); }, [command]);
  // Each section starts at its top: a scroll position from the last one is meaningless here.
  useEffect(() => { bodyRef.current?.scrollTo({ top: 0 }); }, [tab]);

  // A solve finished while the panel was open: show it. Whatever was already done on
  // first sight of the queue is history and must not hijack the selection.
  useEffect(() => {
    const done = (data.queue.data?.items ?? []).filter((j) => j.status === 'done' && j.runName).map((j) => j.runName as string);
    if (seenDone.current === null) {
      if (data.queue.data) seenDone.current = new Set(done);
      return;
    }
    const fresh = done.filter((n) => !seenDone.current!.has(n));
    if (fresh.length === 0) return;
    fresh.forEach((n) => seenDone.current!.add(n));
    setRun(fresh[fresh.length - 1]);
    setNewRun(true);
  }, [data.queue.data]);

  // With no choice made, show the newest run rather than an empty Results tab.
  const newest = data.runs.data?.items[0]?.name ?? null;
  useEffect(() => { if (!run && newest) setRun(newest); }, [run, newest]);

  const active = data.queue.data?.active ?? 0;
  const go = (t: StateTab) => { setTab(t); if (t === 'results') setNewRun(false); };
  const openRun = (name: string) => { setRun(name); setCompare([]); go('results'); };

  return (
    <aside className="state-panel" aria-label="State">
      <div className="state-head">
        <div className="state-tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id}
              className={`state-tab${tab === t.id ? ' is-active' : ''}`} onClick={() => go(t.id)}>
              {t.label}
              {t.id === 'changes' && engineVersion !== null && <span className="chip chip--muted">{engineVersion}</span>}
              {t.id === 'runs' && active > 0 && <span className="chip chip--warn">{active}</span>}
              {t.id === 'results' && newRun && tab !== 'results' && <span className="chip chip--new">new</span>}
              {t.id === 'pinned' && pinned.length > 0 && <span className="chip chip--muted">{pinned.length}</span>}
            </button>
          ))}
        </div>
        <button className="btn-ghost sm" onClick={props.onCollapse} title="Collapse the State panel">Collapse</button>
      </div>
      <div className="state-body" ref={bodyRef}>
        {tab === 'model' && <ModelSection model={data.model} engineVersion={engineVersion} />}
        {tab === 'changes' && (
          <ChangesSection journal={data.journal} focusIds={command?.journalIds ?? []} focusNonce={command?.nonce ?? 0}
            callOf={props.callOf} onFocusStep={props.onFocusStep} />
        )}
        {tab === 'runs' && (
          <RunsSection queue={data.queue} runs={data.runs} selected={run} compare={compare}
            onSelect={openRun}
            onToggleCompare={(n) => setCompare((c) => (c.includes(n) ? c.filter((x) => x !== n) : [...c, n].slice(-5)))}
            onOpenCompare={() => go('results')} />
        )}
        {tab === 'results' && (
          <ResultsSection runs={data.runs} selected={run} onSelect={(n) => { setRun(n); setCompare([]); }}
            compare={compare} onCompare={setCompare} />
        )}
        {tab === 'pinned' && (
          pinned.length === 0
            ? <div className="panel-empty"><p><strong>Nothing pinned.</strong></p><p className="muted">Use Pin on a chart in the conversation to keep it here while the conversation moves on.</p></div>
            : pinned.map((p) => (
              <Chart key={p.id} spec={p.spec} wide height={260} pinned onTogglePin={() => props.onUnpin(p.id)} />
            ))
        )}
      </div>
    </aside>
  );
}
