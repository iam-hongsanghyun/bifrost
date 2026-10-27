/**
 * Which model, how hard it thinks, and how to stop it.
 *
 * All three were reachable only by API. The Stop gap was the worst of them: a turn
 * that hung left no way out of the UI, while `POST /api/agent/interrupt` sat there
 * unused — and the 409 it would have prevented arrived as raw JSON in a banner.
 *
 * Model and effort take effect at the START of a turn (the SDK bakes them into the
 * client at connect time), so the controls disable mid-turn rather than accepting a
 * change that would silently apply one turn later.
 */
import { useEffect, useState } from 'react';
import { AgentStatus, api } from '../api';

interface Props {
  sessionId: string;
  /** True while a turn is running — Stop is live, the pickers are not. */
  busy: boolean;
  onError: (message: string) => void;
}

/** Shorten `claude-opus-5-5` to `opus-5-5`: the vendor prefix is never the choice. */
function shortModel(id: string): string {
  return id.replace(/^claude-/, '');
}

export function AgentControls({ sessionId, busy, onError }: Props) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [stopping, setStopping] = useState(false);

  // Re-read when a turn ends: the server owns cost and state, and a turn that
  // was interrupted must not leave a stale "working" reading behind.
  useEffect(() => {
    let live = true;
    api.agentStatus(sessionId)
      .then((s) => { if (live) setStatus(s); })
      .catch(() => { /* the banner already covers an unreachable backend */ });
    return () => { live = false; };
  }, [sessionId, busy]);

  const stop = async () => {
    setStopping(true);
    try {
      await api.interrupt(sessionId);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Could not stop the turn.');
    } finally {
      setStopping(false);
    }
  };

  const pick = async (what: 'model' | 'effort', value: string) => {
    try {
      if (what === 'model') {
        const next = await api.setModel(value, sessionId);
        setStatus((s) => (s ? { ...s, model: next.model } : s));
      } else {
        const next = await api.setEffort(value, sessionId);
        setStatus((s) => (s ? { ...s, effort: next.effort } : s));
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : `Could not change the ${what}.`);
    }
  };

  if (!status?.available) return null;

  return (
    <div className="agent-controls">
      {busy && (
        <button className="btn-stop" onClick={() => void stop()} disabled={stopping}
          title="Stop this turn — pending approvals are denied and pending questions released">
          {stopping ? 'Stopping…' : 'Stop'}
        </button>
      )}

      <label className="ctl">
        <span className="ctl-label">Model</span>
        <select
          value={status.model ?? ''}
          disabled={busy}
          title={busy ? 'Takes effect at the start of a turn — wait, or press Stop' : status.model ?? 'SDK default'}
          onChange={(e) => void pick('model', e.target.value)}
        >
          {!status.model && <option value="">default</option>}
          {status.models.map((m) => (
            <option key={m} value={m}>{shortModel(m)}</option>
          ))}
        </select>
      </label>

      <label className="ctl">
        <span className="ctl-label">Effort</span>
        <select
          value={status.effort}
          disabled={busy}
          title={busy
            ? 'Takes effect at the start of a turn — wait, or press Stop'
            : 'How hard it thinks before acting. Higher costs more and is usually worth it for a study decision.'}
          onChange={(e) => void pick('effort', e.target.value)}
        >
          {status.efforts.map((e) => (
            <option key={e} value={e}>{e}</option>
          ))}
        </select>
      </label>

      {status.costUsd > 0 && (
        <span className="chip chip--muted" title="Spent in this session">
          ${status.costUsd.toFixed(2)}
        </span>
      )}
    </div>
  );
}
