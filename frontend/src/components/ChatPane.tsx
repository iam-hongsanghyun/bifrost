/**
 * The transcript, and the composer.
 *
 * Tool activity is shown as plain sentences, not tool traffic. The raw stream is
 * honest but unreadable — `mcp__ragnarok__add_generator` with 400 characters of JSON
 * attached, plus SDK plumbing discovering its own toolbox — and none of that tells
 * the user what is happening to their model. So each call becomes one line of
 * English, a finished call reports whether it worked, and a call that changed the
 * model says how many changes it made. That count is the honest part: it comes from
 * Ragnarok's journal, so "it changed something" is checkable rather than claimed.
 *
 * Plumbing is folded into a single quiet line instead of one row each. It is not
 * hidden outright — when the agent spends ten steps looking for a tool, the user
 * deserves to see that time went somewhere.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ChatEvent, UploadedFile } from '../api';
import { prettyBytes } from '../lib/attachments';
import { describeTool, describeToolDone, humanSummary, isPlumbing } from '../lib/toolLabels';
import { Markdown } from '../lib/markdown';
import { Chart, ChartSpec } from './Chart';
import { specFor } from '../lib/chartEvents';

interface Props {
  events: ChatEvent[];
  /** Ask for the points of a stored chart that scrolled into view. */
  onNeedChart: (chartId: string) => void;
  /** Partial assistant text still streaming, if any. */
  streaming: string;
  busy: boolean;
  disabled: boolean;
  disabledReason?: string;
  onSend: (message: string, files: UploadedFile[]) => void;
  /** Store files and return their paths — the agent reads them by path. */
  onUpload: (files: File[]) => Promise<UploadedFile[]>;
  /** The agent BLOCKS on these — an unanswerable question is a dead conversation. */
  onAnswerQuestion: (questionId: string, answer: string) => void;
  onDecideApproval: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Switch to the Ragnarok tab and point it at what the agent named. */
  onShowInRagnarok: (event: ChatEvent) => void;
  /** Chart ids kept in the State panel, and how to keep or drop one. */
  pinnedIds: ReadonlySet<string>;
  onTogglePin: (chartId: string) => void;
  /** Show these journal entries in the State panel's Changes timeline. */
  onOpenJournal: (ids: number[]) => void;
  /** A change in the timeline asked to see the step that made it. */
  focusStep: { callId: string; nonce: number } | null;
}

/** Chips for what the user handed over, on their own message and on a settled card. */
function FileChips({ files }: { files: UploadedFile[] }) {
  return (
    <div className="files">
      {files.map((f) => (
        <span key={f.path} className="file-chip" title={f.path}>
          {f.name} <em>{prettyBytes(f.size)}</em>
        </span>
      ))}
    </div>
  );
}

/** A stored chart before its points arrive: keeps its place, asks for them when seen. */
function ChartStub({ event, onVisible }: { event: ChatEvent; onVisible: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === 'undefined') {
      onVisible();
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        onVisible();
      }
    }, { rootMargin: '400px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [onVisible]);
  return (
    <div ref={ref} className="chart-stub" aria-busy="true">
      {event.title ? `${event.title} - loading chart` : 'Loading chart'}
    </div>
  );
}

/** What the transcript renders: a message, a tool step, or folded plumbing. */
type Row =
  | { kind: 'message'; event: ChatEvent; key: string }
  | { kind: 'tool'; event: ChatEvent; key: string }
  | { kind: 'plumbing'; count: number; key: string }
  | { kind: 'question'; event: ChatEvent; answered: string | null; key: string }
  | { kind: 'approval'; event: ChatEvent; decided: string | null; key: string }
  | { kind: 'chart'; spec: ChartSpec; key: string; chartId: string }
  | { kind: 'chartStub'; event: ChatEvent; key: string; chartId: string }
  | { kind: 'assumption'; event: ChatEvent; key: string }
  | { kind: 'show'; event: ChatEvent; key: string };

/**
 * Collapse the event stream into rows.
 *
 * A tool's start and end are two events describing one step, so the end REPLACES
 * the start rather than adding a line — otherwise every call reads twice. Runs of
 * plumbing collapse into one counted row.
 */
function toRows(events: ChatEvent[]): Row[] {
  const rows: Row[] = [];
  const toolRowIndex = new Map<string, number>();
  // tool.end carries no tool name (only the call id and what it changed), so the
  // name has to come from its tool.start. Without it every end was classed as
  // plumbing and dropped: no tool row ever finished, and no change count showed.
  const startOf = new Map<string, ChatEvent>();
  events.forEach((e) => { if (e.kind === 'tool.start' && e.callId) startOf.set(e.callId, e); });

  // A resolved question or approval must render as settled, not as a live card the
  // user could answer twice — the agent has already moved on.
  const answered = new Map<string, string>();
  const decided = new Map<string, string>();
  events.forEach((e) => {
    if (e.kind === 'question.resolved' && e.questionId) answered.set(e.questionId, e.answer ?? '');
    if (e.kind === 'approval.resolved' && e.approvalId) decided.set(e.approvalId, e.decision ?? '');
  });

  events.forEach((event, index) => {
    if (event.kind === 'question.request' && event.questionId) {
      rows.push({
        kind: 'question', event, key: `q-${event.questionId}`,
        answered: answered.get(event.questionId) ?? null,
      });
      return;
    }
    if (event.kind === 'approval.request' && event.approvalId) {
      rows.push({
        kind: 'approval', event, key: `a-${event.approvalId}`,
        decided: decided.get(event.approvalId) ?? null,
      });
      return;
    }
    if (event.kind === 'chart.show' && event.x && event.series) {
      const chartId = event.chartId ?? `idx-${index}`;
      rows.push({ kind: 'chart', key: `chart-${chartId}`, chartId, spec: specFor(event) });
      return;
    }
    if (event.kind === 'chart.show' && event.lazy && event.chartId && !event.x) {
      rows.push({ kind: 'chartStub', event, key: `chart-${event.chartId}`, chartId: event.chartId });
      return;
    }
    if (event.kind === 'show.request') {
      rows.push({ kind: 'show', event, key: `show-${index}` });
      return;
    }
    if (event.kind === 'assumption.recorded') {
      rows.push({ kind: 'assumption', event, key: `as-${event.id ?? index}` });
      return;
    }
    // The resolutions themselves are folded into the card above.
    if (event.kind === 'question.resolved' || event.kind === 'approval.resolved') return;
    const isTool = event.kind === 'tool.start' || event.kind === 'tool.end';
    const tool = event.tool ?? (event.callId ? startOf.get(event.callId)?.tool : undefined);

    if (isTool && isPlumbing(tool)) {
      // Only count starts, or every call would be counted twice.
      if (event.kind !== 'tool.start') return;
      const last = rows[rows.length - 1];
      if (last?.kind === 'plumbing') last.count += 1;
      else rows.push({ kind: 'plumbing', count: 1, key: `plumbing-${index}` });
      return;
    }

    if (isTool) {
      const id = event.callId ?? `tool-${index}`;
      const existing = toolRowIndex.get(id);
      if (existing !== undefined && event.kind === 'tool.end') {
        rows[existing] = { kind: 'tool', event: { ...event, tool }, key: `tool-${id}` };
        return;
      }
      if (existing !== undefined) return;
      toolRowIndex.set(id, rows.length);
      rows.push({ kind: 'tool', event, key: `tool-${id}` });
      return;
    }

    const text = event.markdown ?? event.text ?? '';
    if (!text) return;
    rows.push({ kind: 'message', event, key: event.messageId ?? `msg-${index}` });
  });

  return rows;
}

function QuestionCard(
  { event, answered, onAnswer, onUpload }:
  {
    event: ChatEvent; answered: string | null;
    onAnswer: (id: string, answer: string) => void;
    onUpload: (files: File[]) => Promise<UploadedFile[]>;
  },
) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const pickerRef = useRef<HTMLInputElement>(null);
  const id = event.questionId ?? '';
  const options = event.options ?? [];

  // Answering WITH a file: store it, then answer with the paths, because the
  // agent is blocked on this question and cannot go looking for an upload.
  const answerWithFiles = async (picked: File[]) => {
    if (picked.length === 0) return;
    setBusy(true);
    setFailed(null);
    try {
      const stored = await onUpload(picked);
      const lines = stored.map((f) => `- ${f.name} → ${f.path} (${prettyBytes(f.size)})`);
      onAnswer(id, `I uploaded the file(s) you asked for:\n${lines.join('\n')}`);
    } catch (e) {
      // Do NOT answer on failure — a wrong answer unblocks the agent with a path
      // that is not there, and it would then invent the data instead.
      setFailed(e instanceof Error ? e.message : 'Upload failed.');
    } finally {
      setBusy(false);
    }
  };

  if (answered !== null) {
    return (
      <div className="card card--settled">
        <p className="card-q">{event.question}</p>
        <p className="card-a">You answered: <strong>{answered || '(no answer)'}</strong></p>
      </div>
    );
  }
  return (
    <div className="card">
      <p className="card-q">{event.question}</p>
      {event.needsFile && (
        <div className="card-file">
          <input
            ref={pickerRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              void answerWithFiles(Array.from(e.target.files ?? []));
              e.target.value = '';  // so the same file can be picked twice
            }}
          />
          <button className="btn-primary" disabled={busy} onClick={() => pickerRef.current?.click()}>
            {busy ? 'Uploading…' : 'Choose a file…'}
          </button>
          <span className="muted">CSV, JSON or text. Or answer below instead.</span>
          {failed && <span className="card-fail">{failed}</span>}
        </div>
      )}
      {options.length > 0 && (
        <div className="card-options">
          {options.map((option) => (
            <button key={option} className="btn-ghost" onClick={() => onAnswer(id, option)}>
              {option}
            </button>
          ))}
        </div>
      )}
      <div className="card-free">
        <input
          value={text}
          placeholder="Or answer in your own words…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) onAnswer(id, text.trim()); }}
        />
        <button className="btn-primary" disabled={!text.trim()} onClick={() => onAnswer(id, text.trim())}>
          Answer
        </button>
      </div>
    </div>
  );
}

function ApprovalCard(
  { event, decided, onDecide }:
  { event: ChatEvent; decided: string | null; onDecide: (id: string, d: 'allow' | 'deny') => void },
) {
  const id = event.approvalId ?? '';
  const args = event.args ?? {};
  const shown = Object.entries(args).slice(0, 6);

  if (decided !== null) {
    return (
      <div className="card card--settled">
        <p className="card-q">{describeTool(event.tool)}</p>
        <p className="card-a">You chose: <strong>{decided}</strong></p>
      </div>
    );
  }
  return (
    <div className="card">
      <p className="card-q">Allow this? <strong>{describeTool(event.tool)}</strong></p>
      {shown.length > 0 && (
        <dl className="card-args">
          {shown.map(([k, v]) => (
            <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>
          ))}
        </dl>
      )}
      <div className="card-options">
        <button className="btn-primary" onClick={() => onDecide(id, 'allow')}>Allow</button>
        <button className="btn-ghost" onClick={() => onDecide(id, 'deny')}>Deny</button>
      </div>
    </div>
  );
}

function ToolRow(
  { event, flash, onOpenJournal }:
  { event: ChatEvent; flash: boolean; onOpenJournal: (ids: number[]) => void },
) {
  const done = event.kind === 'tool.end';
  const ok = event.ok !== false;
  const ids = event.journalEntryIds ?? [];
  const changed = ids.length;
  const summary = done ? humanSummary(event.resultSummary) : null;

  return (
    <div data-call-id={event.callId}
      className={`tool-row${done ? (ok ? ' is-done' : ' is-error') : ' is-running'}${flash ? ' is-flash' : ''}`}>
      <span className="tool-icon" aria-hidden>{done ? (ok ? '✓' : '✕') : '·'}</span>
      <span className="tool-what">
        {done ? describeToolDone(event.tool, ok) : `${describeTool(event.tool)}…`}
      </span>
      {summary && <span className="tool-summary">{summary}</span>}
      {changed > 0 && (
        <button className="chip chip--link" onClick={() => onOpenJournal(ids)}
          title="Recorded in Ragnarok's change timeline. Click to see what it changed.">
          {changed} {changed === 1 ? 'change' : 'changes'}
        </button>
      )}
    </div>
  );
}

export function ChatPane(props: Props) {
  const { events, streaming, busy, disabled, disabledReason } = props;
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const pickerRef = useRef<HTMLInputElement>(null);
  const rows = useMemo(() => toRows(events), [events]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [flashId, setFlashId] = useState<string | null>(null);

  const take = async (picked: File[]) => {
    if (picked.length === 0 || disabled) return;
    setUploading(true);
    setUploadError(null);
    try {
      const stored = await props.onUpload(picked);
      setPending((prev) => [...prev, ...stored]);
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : 'Could not store that file.');
    } finally {
      setUploading(false);
    }
  };

  // The change timeline asked for the step that made a change: scroll to it, flash it.
  useEffect(() => {
    const target = props.focusStep;
    if (!target) return undefined;
    const el = scrollRef.current?.querySelector(`[data-call-id="${CSS.escape(target.callId)}"]`);
    if (!el) return undefined;
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    setFlashId(target.callId);
    const timer = window.setTimeout(() => setFlashId(null), 2200);
    return () => window.clearTimeout(timer);
  }, [props.focusStep]);

  // Follow the tail as it streams. Both move, so both are dependencies —
  // otherwise the view sticks mid-answer.
  useEffect(() => {
    if (props.focusStep) return; // do not yank the view back while showing a step
    endRef.current?.scrollIntoView({ block: 'end' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.length, streaming]);

  const submit = () => {
    const message = draft.trim();
    // A bare attachment is a legitimate message — "here is the wind profile" with
    // no words. Only an empty draft AND no files is nothing to send.
    if ((!message && pending.length === 0) || busy || disabled) return;
    setDraft('');
    setPending([]);
    setUploadError(null);
    props.onSend(message, pending);
  };

  return (
    <section
      className={`chat${dragging ? ' is-dropping' : ''}`}
      onDragOver={(e) => { e.preventDefault(); if (!disabled) setDragging(true); }}
      onDragLeave={(e) => { e.preventDefault(); setDragging(false); }}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        void take(Array.from(e.dataTransfer.files ?? []));
      }}
    >
      <div className="chat-scroll" ref={scrollRef}>
        {rows.length === 0 && !streaming && (
          <div className="chat-empty">
            <h2>Ask for a model</h2>
            <p className="muted">
              The agent drives Ragnarok — build a network, attach profiles, solve, compare.
              Switch to the <strong>Ragnarok</strong> tab at any point to see the model it is
              working on. Drop a CSV in to hand over your own data — a wind profile, a load
              trace, a cost table.
            </p>
          </div>
        )}

        {rows.map((row) => {
          if (row.kind === 'plumbing') {
            return (
              <div key={row.key} className="tool-row is-plumbing">
                <span className="tool-icon" aria-hidden>·</span>
                <span className="tool-what">
                  {row.count === 1 ? 'Looking up its tools' : `Looking up its tools (${row.count} steps)`}
                </span>
              </div>
            );
          }
          if (row.kind === 'question') {
            return (
              <QuestionCard key={row.key} event={row.event} answered={row.answered}
                onAnswer={(id, answer) => void props.onAnswerQuestion(id, answer)}
                onUpload={props.onUpload} />
            );
          }
          if (row.kind === 'approval') {
            return (
              <ApprovalCard key={row.key} event={row.event} decided={row.decided}
                onDecide={(id, d) => void props.onDecideApproval(id, d)} />
            );
          }
          if (row.kind === 'assumption') {
            const a = row.event;
            return (
              <div key={row.key} className={`assume${a.basis === 'assumed' ? ' assume--own' : ''}`}>
                <span className="assume-tag">{a.basis}</span>
                <span className="assume-body">
                  <strong>{a.subject}</strong> = {a.value}{a.unit ? ` ${a.unit}` : ''}
                  {a.source && <em> — {a.source}</em>}
                </span>
              </div>
            );
          }
          if (row.kind === 'show') {
            const e = row.event;
            const what = e.select || e.tab || 'the model';
            return (
              <button
                key={row.key}
                className="show-row"
                onClick={() => props.onShowInRagnarok(e)}
                title={`Switch to the Ragnarok ${e.tab} tab${e.select ? ` and highlight ${e.select}` : ''}`}
              >
                <span className="show-icon" aria-hidden>→</span>
                <span>
                  Show me <strong>{what}</strong>
                  {e.why && <em> — {e.why}</em>}
                </span>
              </button>
            );
          }
          if (row.kind === 'chartStub') {
            return <ChartStub key={row.key} event={row.event} onVisible={() => props.onNeedChart(row.chartId)} />;
          }
          if (row.kind === 'chart') {
            return (
              <Chart key={row.key} spec={row.spec} pinned={props.pinnedIds.has(row.chartId)}
                onTogglePin={() => props.onTogglePin(row.chartId)} />
            );
          }
          if (row.kind === 'tool') {
            return (
              <ToolRow key={row.key} event={row.event} onOpenJournal={props.onOpenJournal}
                flash={flashId !== null && flashId === row.event.callId} />
            );
          }
          const text = row.event.markdown ?? row.event.text ?? '';
          const mine = row.event.role === 'user';
          const attached = row.event.files ?? [];
          return (
            <article key={row.key} className={`bubble${mine ? ' bubble--user' : ''}`}>
              {mine ? text : <Markdown text={text} />}
              {attached.length > 0 && <FileChips files={attached} />}
            </article>
          );
        })}

        {streaming && <article className="bubble bubble--streaming">{streaming}</article>}
        <div ref={endRef} />
      </div>

      <footer className="composer">
        {(pending.length > 0 || uploading || uploadError) && (
          <div className="composer-files">
            {pending.map((f) => (
              <span key={f.path} className="file-chip" title={f.path}>
                {f.name} <em>{prettyBytes(f.size)}</em>
                <button
                  className="file-drop"
                  aria-label={`Remove ${f.name}`}
                  onClick={() => setPending((prev) => prev.filter((p) => p.path !== f.path))}
                >
                  ×
                </button>
              </span>
            ))}
            {uploading && <span className="muted">Storing…</span>}
            {uploadError && <span className="card-fail">{uploadError}</span>}
          </div>
        )}
        <input
          ref={pickerRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            void take(Array.from(e.target.files ?? []));
            e.target.value = '';
          }}
        />
        <button
          className="btn-attach"
          aria-label="Attach a data file"
          title="Attach a data file — CSV, JSON or text"
          disabled={disabled || uploading}
          onClick={() => pickerRef.current?.click()}
        >
          +
        </button>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline. The chat convention, not a form's.
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={disabled ? (disabledReason ?? 'Unavailable') : 'Describe what you want modelled…'}
          disabled={disabled || busy}
          rows={3}
        />
        <button
          className="btn-primary"
          onClick={submit}
          disabled={disabled || busy || (!draft.trim() && pending.length === 0)}
        >
          {busy ? 'Working…' : 'Send'}
        </button>
      </footer>
    </section>
  );
}
