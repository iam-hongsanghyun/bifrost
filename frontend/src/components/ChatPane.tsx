/**
 * The transcript, and the composer.
 *
 * Tool calls are rendered inline rather than hidden, and a finished call shows the
 * Ragnarok journal entries it produced — so "the agent changed something" is a
 * claim the user can check against the engine rather than take on trust.
 */
import { useEffect, useRef, useState } from 'react';
import { ChatEvent } from '../api';

interface Props {
  events: ChatEvent[];
  /** Partial assistant text still streaming, if any. */
  streaming: string;
  busy: boolean;
  disabled: boolean;
  disabledReason?: string;
  onSend: (message: string) => void;
}

function ToolRow({ event }: { event: ChatEvent }) {
  const done = event.kind === 'tool.end';
  const changed = event.journalEntryIds ?? [];
  return (
    <div className={`tool-row${done ? (event.ok === false ? ' is-error' : ' is-done') : ' is-running'}`}>
      <span className="tool-name">{event.tool ?? 'tool'}</span>
      <span className="tool-state">
        {done ? (event.ok === false ? 'failed' : 'done') : 'running…'}
      </span>
      {event.resultSummary && <span className="tool-summary">{event.resultSummary}</span>}
      {changed.length > 0 && (
        <span className="chip" title="Ragnarok journal entries this step produced">
          changed {changed.length}
        </span>
      )}
    </div>
  );
}

export function ChatPane(props: Props) {
  const { events, streaming, busy, disabled, disabledReason } = props;
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  // Follow the tail as it streams. `events.length` and `streaming` both move, so
  // both are dependencies — otherwise the view sticks mid-answer.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [events.length, streaming]);

  const submit = () => {
    const message = draft.trim();
    if (!message || busy || disabled) return;
    setDraft('');
    props.onSend(message);
  };

  return (
    <section className="chat">
      <div className="chat-scroll">
        {events.length === 0 && !streaming && (
          <div className="chat-empty">
            <h2>Ask for a model</h2>
            <p className="muted">
              The agent drives Ragnarok — build a network, attach profiles, solve, compare.
              Switch to the <strong>Ragnarok</strong> tab at any point to see the model it is
              working on.
            </p>
          </div>
        )}

        {events.map((event, index) => {
          if (event.kind === 'tool.start' || event.kind === 'tool.end') {
            return <ToolRow key={`${event.callId}-${event.kind}-${index}`} event={event} />;
          }
          const text = event.markdown ?? event.text ?? '';
          if (!text) return null;
          const mine = event.role === 'user';
          return (
            <article key={event.messageId ?? index} className={`bubble${mine ? ' bubble--user' : ''}`}>
              {text}
            </article>
          );
        })}

        {streaming && <article className="bubble bubble--streaming">{streaming}</article>}
        <div ref={endRef} />
      </div>

      <footer className="composer">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline. Matches the chat convention
            // rather than a form's.
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={disabled ? (disabledReason ?? 'Unavailable') : 'Describe what you want modelled…'}
          disabled={disabled || busy}
          rows={3}
        />
        <button className="btn-primary" onClick={submit} disabled={disabled || busy || !draft.trim()}>
          {busy ? 'Working…' : 'Send'}
        </button>
      </footer>
    </section>
  );
}
