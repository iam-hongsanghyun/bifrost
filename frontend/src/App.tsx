/**
 * The shell: a tab bar over the conversation and the engine.
 *
 * Two surfaces, one session. The Conversation tab is the rail plus the transcript;
 * the Ragnarok tab is the real Ragnarok application in an iframe. Nothing is
 * reimplemented, so the user confirms the agent's work in the tool they already
 * know, and it cannot drift from the engine's own behaviour.
 *
 * The iframe is mounted ONCE and hidden with CSS rather than unmounted on tab
 * switch. Remounting would reload Ragnarok — losing its scroll, its open sheet and
 * its map viewport every time the user glanced at the chat.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  api, ChatEvent, ClientConfig, Conversation, ConversationGroup, EngineInfo, engineApi, Health,
  subscribeChat, subscribeEngine,
} from './api';
import { ChatPane } from './components/ChatPane';
import { ConversationRail } from './components/ConversationRail';
import { EngineSetup } from './components/EngineSetup';

type Tab = 'conversation' | 'ragnarok';

export default function App() {
  const [tab, setTab] = useState<Tab>('conversation');
  const [config, setConfig] = useState<ClientConfig | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [groups, setGroups] = useState<ConversationGroup[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [streaming, setStreaming] = useState('');
  const [busy, setBusy] = useState(false);
  const [engineVersion, setEngineVersion] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [engine, setEngineInfo] = useState<EngineInfo | null>(null);
  // Setup is a GATE, not a tab: with no engine there is no model to talk about, so
  // showing the chat first would only offer a conversation that cannot do anything.
  const [showSetup, setShowSetup] = useState(false);

  // The transcript is persisted as it grows, not only on close — a crashed tab
  // must not cost the conversation. Kept in a ref so the debounce does not need
  // `events` as a dependency and restart the timer on every token.
  const eventsRef = useRef<ChatEvent[]>([]);
  eventsRef.current = events;

  const refreshRail = useCallback(async () => {
    const { groups: next } = await api.listConversations();
    setGroups(next);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const [cfg, hp, eng] = await Promise.all([api.config(), api.health(), engineApi.get()]);
        setConfig(cfg);
        setHealth(hp);
        setEngineInfo(eng);
        // Open on setup until an engine is both chosen AND up.
        setShowSetup(!eng.configured || !eng.process.running);
        await refreshRail();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Bifrost is not reachable.');
      }
    })();
  }, [refreshRail]);

  // Bifrost's stream: the conversation.
  useEffect(() => {
    if (!config) return undefined;
    return subscribeChat(config.sessionId, (kind, data) => {
      if (kind === 'chat.delta') {
        setStreaming((prev) => prev + (data.text ?? ''));
        return;
      }
      if (kind === 'chat.message') {
        setStreaming('');
        setEvents((prev) => [...prev, data]);
        return;
      }
      if (kind === 'agent.status') {
        // The service reports idle when a turn completes; nothing to append.
        setBusy(Boolean((data as unknown as { state?: string }).state &&
          (data as unknown as { state?: string }).state !== 'idle'));
        return;
      }
      setEvents((prev) => [...prev, data]);
    });
  }, [config]);

  // Ragnarok's stream: the model. Straight from the engine — Bifrost forwards
  // nothing, which is what keeps the Ragnarok tab current.
  useEffect(() => {
    if (!config) return undefined;
    return subscribeEngine(config.ragnarokBase, config.sessionId, setEngineVersion);
  }, [config]);

  // Debounced transcript save.
  useEffect(() => {
    if (!activeId || events.length === 0) return undefined;
    const timer = window.setTimeout(() => {
      void api.saveEvents(activeId, eventsRef.current).then(refreshRail).catch(() => {
        /* best-effort: a failed save must not interrupt the chat */
      });
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [activeId, events.length, refreshRail]);

  const refreshEngine = useCallback(async () => {
    const [eng, hp] = await Promise.all([engineApi.get(), api.health()]);
    setEngineInfo(eng);
    setHealth(hp);
    if (eng.configured && eng.process.running) setShowSetup(false);
  }, []);

  const select = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      // Switching conversation switches worlds — this loads its project package
      // into the shared session.
      const opened = await api.activate(id);
      setActiveId(id);
      setEvents(opened.events ?? []);
      setStreaming('');
      await refreshRail();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open that conversation.');
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    const conversation = await api.createConversation('New conversation', 'Unfiled');
    await refreshRail();
    await select(conversation.id);
  };

  const rename = async (conversation: Conversation) => {
    const title = window.prompt('Conversation title', conversation.title);
    if (title === null) return;
    await api.update(conversation.id, { title });
    await refreshRail();
  };

  const regroup = async (conversation: Conversation) => {
    const group = window.prompt('Group', conversation.group);
    if (group === null) return;
    await api.update(conversation.id, { group });
    await refreshRail();
  };

  const remove = async (conversation: Conversation) => {
    // Deleting takes the model with it — the package is the only copy once the
    // session has moved on, so this asks first.
    const ok = window.confirm(
      `Delete “${conversation.title}”?\n\nThis also deletes its saved model, which cannot be recovered.`,
    );
    if (!ok) return;
    await api.remove(conversation.id);
    if (activeId === conversation.id) {
      setActiveId(null);
      setEvents([]);
    }
    await refreshRail();
  };

  const snapshot = async (conversation: Conversation) => {
    setBusy(true);
    try {
      await api.snapshot(conversation.id);
      await refreshRail();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the model.');
    } finally {
      setBusy(false);
    }
  };

  const send = async (message: string) => {
    if (!config || !activeId) return;
    setEvents((prev) => [...prev, { kind: 'chat.message', role: 'user', markdown: message }]);
    setBusy(true);
    try {
      await api.chat(message, activeId, config.sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The agent did not accept that message.');
      setBusy(false);
    }
  };

  const engineDown = health?.engine.reachable === false;
  const chatDisabled = !activeId || engineDown;
  const chatDisabledReason = engineDown
    ? 'Ragnarok is not running — start it, then reload.'
    : !activeId
      ? 'Pick or create a conversation first.'
      : undefined;

  if (showSetup && engine) {
    return <EngineSetup info={engine} onChanged={() => void refreshEngine()} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Bifrost</span>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'conversation'}
            className={`tab${tab === 'conversation' ? ' is-active' : ''}`}
            onClick={() => setTab('conversation')}>
            Conversation
          </button>
          <button role="tab" aria-selected={tab === 'ragnarok'}
            className={`tab${tab === 'ragnarok' ? ' is-active' : ''}`}
            onClick={() => setTab('ragnarok')}>
            Ragnarok
            {engineVersion !== null && <span className="chip chip--muted">v{engineVersion}</span>}
          </button>
        </div>
        <span className="topbar-right muted">
          {engineDown ? 'engine offline' : config ? `session ${config.sessionId}` : ''}
          <button className="btn-ghost sm" onClick={() => setShowSetup(true)} title="Engine settings">
            engine
          </button>
        </span>
      </header>

      {error && (
        <div className="banner banner--error" role="alert">
          {error}
          <button className="btn-ghost sm" onClick={() => setError(null)}>dismiss</button>
        </div>
      )}
      {engineDown && (
        <div className="banner" role="status">
          Ragnarok is not reachable at <code>{health?.engine.base}</code>. The Ragnarok tab and the
          agent both need it.
        </div>
      )}

      <main className="body">
        <div className={`pane${tab === 'conversation' ? '' : ' is-hidden'}`}>
          <ConversationRail
            groups={groups}
            activeId={activeId}
            engineVersion={engineVersion}
            busy={busy}
            onSelect={(id) => void select(id)}
            onCreate={() => void create()}
            onRename={(c) => void rename(c)}
            onRegroup={(c) => void regroup(c)}
            onDelete={(c) => void remove(c)}
            onSnapshot={(c) => void snapshot(c)}
          />
          <ChatPane
            events={events}
            streaming={streaming}
            busy={busy}
            disabled={chatDisabled}
            disabledReason={chatDisabledReason}
            onSend={(m) => void send(m)}
          />
        </div>

        {/* Mounted once, hidden on the other tab — remounting would reload
            Ragnarok and lose its scroll, open sheet and map viewport. */}
        <div className={`pane pane--frame${tab === 'ragnarok' ? '' : ' is-hidden'}`}>
          {config && (
            <iframe className="engine-frame" src={config.ragnarokBase} title="Ragnarok" />
          )}
        </div>
      </main>
    </div>
  );
}
