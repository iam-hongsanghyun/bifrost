/**
 * The live data behind the State panel.
 *
 * Two triggers keep it current, because Ragnarok has two kinds of change:
 *  - the MODEL and the JOURNAL change through the session, and Ragnarok announces
 *    every such change as `session.version` on its event stream (the shell already
 *    subscribes) — so those two refetch when `engineVersion` moves;
 *  - the QUEUE and RUNS change inside Ragnarok's worker with no event, so the queue
 *    is polled — fast while a job is queued or running, slow otherwise, and not at
 *    all while the tab is hidden — and the run list refetches when a job finishes.
 */
import { useEffect, useMemo } from 'react';
import { JournalPage, ListResponse, QueueState, RunMeta, StateModel, stateApi } from '../api';
import { Resource, useResource, useVisible } from './useResource';

const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 6000;
const REFRESH_DEBOUNCE_MS = 250;

export interface StateData {
  model: Resource<StateModel>;
  journal: Resource<JournalPage>;
  queue: Resource<QueueState>;
  runs: Resource<ListResponse<RunMeta>>;
}

export function useStateData(enabled: boolean, engineVersion: number | null): StateData {
  const visible = useVisible();
  const live = enabled && visible;

  const model = useResource(() => stateApi.model(), [engineVersion], live, REFRESH_DEBOUNCE_MS);
  const journal = useResource(() => stateApi.journal(100), [engineVersion], live, REFRESH_DEBOUNCE_MS);
  const queue = useResource(() => stateApi.queue(), [], live);

  // Poll the queue; the cadence depends on whether anything is in flight.
  const active = (queue.data?.active ?? 0) > 0;
  const reloadQueue = queue.reload;
  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setInterval(reloadQueue, active ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    return () => window.clearInterval(timer);
  }, [live, active, reloadQueue]);

  // A finished job is a new run: refetch the list when the set of finished jobs changes.
  const doneKey = useMemo(
    () => (queue.data?.items ?? []).filter((j) => j.status === 'done').map((j) => j.runName ?? j.id).join('|'),
    [queue.data],
  );
  const runs = useResource(() => stateApi.runs(), [doneKey], live);

  return { model, journal, queue, runs };
}
