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
import { useEffect, useRef, useState } from 'react';
import { ChatEvent, UploadedFile } from '../api';
import { prettyBytes } from '../lib/attachments';
import { describeTool, describeToolDone, humanSummary, isPlumbing } from '../lib/toolLabels';
import { Markdown } from '../lib/markdown';
import { Chart, ChartSpec } from './Chart';

interface Props {
  events: ChatEvent[];
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

/** What the transcript renders: a message, a tool step, or folded plumbing. */
type Row =
  | { kind: 'message'; event: ChatEvent; key: string }
  | { kind: 'tool'; event: ChatEvent; key: string }
  | { kind: 'plumbing'; count: number; key: string }
  | { kind: 'question'; event: ChatEvent; answered: string | null; key: string }
  | { kind: 'approval'; event: ChatEvent; decided: string | null; key: string }
  | { kind: 'chart'; spec: ChartSpec; key: string }
  | { kind: 'assumption'; event: ChatEvent; key: string };

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
      rows.push({
        kind: 'chart', key: `chart-${event.chartId ?? index}`,
        spec: {
          chartId: event.chartId,
          title: event.title,
          kind: event.chartKind,
          unit: event.unit,
          note: event.note,
          x: event.x,
          series: event.series,
        },
      });
      return;
    }
    if (event.kind === 'assumption.recorded') {
      rows.push({ kind: 'assumption', event, key: `as-${event.id ?? index}` });
      return;
    }
    // The resolutions themselves are folded into the card above.
    if (event.kind === 'question.resolved' || event.kind === 'approval.resolved') return;
    const isTool = event.kind === 'tool.start' || event.kind === 'tool.end';

    if (isTool && isPlumbing(event.tool)) {
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
        rows[existing] = { kind: 'tool', event, key: `tool-${id}` };
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

function ToolRow({ event }: { event: ChatEvent }) {
  const done = event.kind === 'tool.end';
  const ok = event.ok !== false;
  const changed = event.journalEntryIds?.length ?? 0;
  const summary = done ? humanSummary(event.resultSummary) : null;

  return (
    <div className={`tool-row${done ? (ok ? ' is-done' : ' is-error') : ' is-running'}`}>
      <span className="tool-icon" aria-hidden>{done ? (ok ? '✓' : '✕') : '·'}</span>
      <span className="tool-what">
        {done ? describeToolDone(event.tool, ok) : `${describeTool(event.tool)}…`}
      </span>
      {summary && <span className="tool-summary">{summary}</span>}
      {changed > 0 && (
        <span className="chip" title="Recorded in Ragnarok's change timeline">
          {changed} {changed === 1 ? 'change' : 'changes'}
        </span>
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
  const rows = toRows(events);

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

  // Follow the tail as it streams. Both move, so both are dependencies —
  // otherwise the view sticks mid-answer.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
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
      <div className="chat-scroll">
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
          if (row.kind === 'chart') return <Chart key={row.key} spec={row.spec} />;
          if (row.kind === 'tool') return <ToolRow key={row.key} event={row.event} />;
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
