/**
 * The left rail: every conversation, bucketed by group.
 *
 * Selecting one is not a UI state change — it loads that conversation's project
 * package into the shared Ragnarok session, so the Ragnarok tab and the agent are
 * describing the same model again. The rail therefore shows whether a conversation
 * HAS a package, and whether the live session has moved on since it was captured:
 * a transcript describing a model is only trustworthy if the model still matches.
 */
import { Conversation, ConversationGroup } from '../api';

interface Props {
  /** Narrow screens: float over the chat instead of sitting beside it. */
  overlay?: boolean;
  groups: ConversationGroup[];
  activeId: string | null;
  /** Ragnarok's current journal version, or null when nothing is loaded. */
  engineVersion: number | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (conversation: Conversation) => void;
  onRegroup: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
  onSnapshot: (conversation: Conversation) => void;
}

/** Whether the stored package still matches what Ragnarok holds.
 *
 *  `null` engineVersion means nothing is loaded, which is not staleness — it is
 *  simply unknown, and saying "stale" there would be a false alarm. */
function isStale(conversation: Conversation, engineVersion: number | null): boolean {
  if (!conversation.hasProject || engineVersion === null) return false;
  if (conversation.projectVersion === null) return true;
  return engineVersion > conversation.projectVersion;
}

export function ConversationRail(props: Props) {
  const { groups, activeId, engineVersion, busy } = props;
  const empty = groups.every((g) => g.conversations.length === 0);

  return (
    <nav className={`rail${props.overlay ? " rail--overlay" : ""}`} aria-label="Conversations">
      <header className="rail-head">
        <span className="rail-title">Conversations</span>
        <button className="btn-ghost" onClick={props.onCreate} disabled={busy} title="New conversation">
          +
        </button>
      </header>

      <div className="rail-body">
        {empty && <p className="muted rail-empty">No conversations yet. Start one with +.</p>}

        {groups.map((group) => (
          <section key={group.group} className="rail-group">
            <h3 className="rail-group-label">{group.group}</h3>
            {group.conversations.map((conversation) => {
              const active = conversation.id === activeId;
              const stale = isStale(conversation, engineVersion);
              return (
                <div key={conversation.id} className={`rail-item${active ? ' is-active' : ''}`}>
                  <button
                    className="rail-item-main"
                    onClick={() => props.onSelect(conversation.id)}
                    disabled={busy}
                    title={
                      conversation.hasProject
                        ? stale
                          ? 'Opening this restores its saved model — which is older than what Ragnarok holds now'
                          : 'Opening this restores its saved model'
                        : 'No model saved yet — opening this clears the session'
                    }
                  >
                    <span className="rail-item-title">{conversation.title}</span>
                    <span className="rail-item-meta">
                      {conversation.eventCount > 0 && <span>{conversation.eventCount} msg</span>}
                      {conversation.hasProject ? (
                        <span className={stale ? 'chip chip--warn' : 'chip'}>
                          {stale ? 'model behind' : 'model saved'}
                        </span>
                      ) : (
                        <span className="chip chip--muted">no model</span>
                      )}
                    </span>
                  </button>
                  {active && (
                    <div className="rail-item-actions">
                      <button className="btn-ghost sm" onClick={() => props.onSnapshot(conversation)} disabled={busy}
                        title="Save Ragnarok's current model into this conversation">
                        Save model
                      </button>
                      <button className="btn-ghost sm" onClick={() => props.onRename(conversation)} disabled={busy}>
                        Rename
                      </button>
                      <button className="btn-ghost sm" onClick={() => props.onRegroup(conversation)} disabled={busy}>
                        Group
                      </button>
                      <a className="btn-ghost sm" href={`/api/conversations/${conversation.id}/project`}
                        title="Download the project package — it opens in Ragnarok directly">
                        Export
                      </a>
                      <button className="btn-ghost sm danger" onClick={() => props.onDelete(conversation)} disabled={busy}>
                        Delete
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        ))}
      </div>
    </nav>
  );
}
