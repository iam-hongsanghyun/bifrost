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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  api, ChatEvent, ClientConfig, Conversation, ConversationGroup, EngineInfo, engineApi, Health,
  subscribeChat, subscribeEngine, UploadedFile,
} from './api';
import { AgentControls } from './components/AgentControls';
import { ChatPane } from './components/ChatPane';
import { ConversationRail } from './components/ConversationRail';
import { EngineSetup } from './components/EngineSetup';
import { SplitPane } from './components/state/SplitPane';
import { PinnedChart, StateCommand, StatePanel } from './components/state/StatePanel';
import { specFor } from './lib/chartEvents';
import { useStateData } from './lib/useStateData';
import { loadBlob, saveBlob, useMediaQuery } from './lib/useResource';
import { attachmentBlockFor } from './lib/attachments';
import { isWorkingState } from './lib/toolLabels';

type Tab = 'conversation' | 'ragnarok';

/** Layout the user chose, kept across reloads. Versioned: see loadBlob. */
const LAYOUT_KEY = 'bifrost.layout';
const LAYOUT_VERSION = 1;
const PINS_KEY = 'bifrost.pins';
const PINS_VERSION = 1;
const PANEL_DEFAULT_PX = 560;
const PANEL_MIN_PX = 340;
/** At or below this the State panel is a drawer over the chat, not a column beside it. */
const NARROW_QUERY = '(max-width: 960px)';
const RAIL_NARROW_QUERY = '(max-width: 720px)';

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

  // ── layout: the State panel beside the chat ─────────────────────────────
  const narrow = useMediaQuery(NARROW_QUERY);
  const railNarrow = useMediaQuery(RAIL_NARROW_QUERY);
  const saved = useRef(loadBlob(LAYOUT_KEY, LAYOUT_VERSION, { open: true, width: PANEL_DEFAULT_PX }));
  const [panelOpenWide, setPanelOpenWide] = useState(saved.current.open);
  const [panelOpenNarrow, setPanelOpenNarrow] = useState(false);
  const [panelWidth, setPanelWidth] = useState(saved.current.width);
  const [railOpen, setRailOpen] = useState(false);
  const panelOpen = narrow ? panelOpenNarrow : panelOpenWide;
  const setPanelOpen = (open: boolean) => (narrow ? setPanelOpenNarrow(open) : setPanelOpenWide(open));
  useEffect(() => {
    saveBlob(LAYOUT_KEY, LAYOUT_VERSION, { open: panelOpenWide, width: panelWidth });
  }, [panelOpenWide, panelWidth]);
  const bodyRef = useRef<HTMLDivElement>(null);
  const maxPanel = () => Math.max(PANEL_MIN_PX, Math.round((bodyRef.current?.clientWidth ?? 1200) * 0.7));

  // ── the State panel's data, the pins, and the links to and from the chat ──
  const [command, setCommand] = useState<StateCommand | null>(null);
  const [focusStep, setFocusStep] = useState<{ callId: string; nonce: number } | null>(null);
  const [pinIds, setPinIds] = useState<string[]>(() => loadBlob<string[]>(PINS_KEY, PINS_VERSION, []));
  useEffect(() => saveBlob(PINS_KEY, PINS_VERSION, pinIds), [pinIds]);

  // The transcript is persisted as it grows, not only on close — a crashed tab
  // must not cost the conversation. Kept in a ref so the debounce does not need
  // `events` as a dependency and restart the timer on every token.
  const frameRef = useRef<HTMLIFrameElement>(null);
  const eventsRef = useRef<ChatEvent[]>([]);
  eventsRef.current = events;

  const stateData = useStateData(tab === 'conversation' && panelOpen && !showSetup, engineVersion);
  // Pins are stored as chart ids only; the data is whatever the transcript holds.
  // A pin whose chart is no longer in this conversation simply is not shown.
  const pinned = useMemo<PinnedChart[]>(() => {
    const byId = new Map<string, PinnedChart>();
    events.forEach((e) => {
      if (e.kind === 'chart.show' && e.chartId && e.x && e.series && pinIds.includes(e.chartId)) {
        byId.set(e.chartId, { id: e.chartId, spec: specFor(e) });
      }
    });
    return pinIds.flatMap((id) => (byId.has(id) ? [byId.get(id) as PinnedChart] : []));
  }, [events, pinIds]);
  const callOf = useMemo(() => {
    const map = new Map<number, string>();
    events.forEach((e) => {
      if (e.kind === 'tool.end' && e.callId) (e.journalEntryIds ?? []).forEach((id) => map.set(id, e.callId as string));
    });
    return map;
  }, [events]);
  // Stored charts open as stubs; fetch the points of the ones that are drawn (visible
  // in the transcript) or pinned. One request per chart, never repeated.
  const chartsRequested = useRef(new Set<string>());
  useEffect(() => { chartsRequested.current = new Set(); }, [activeId]);
  const loadChart = useCallback((chartId: string) => {
    const conversationId = activeId;
    if (!conversationId || chartsRequested.current.has(chartId)) return;
    chartsRequested.current.add(chartId);
    api.chart(conversationId, chartId)
      .then((points) => setEvents((prev) => prev.map((e) => (
        e.kind === 'chart.show' && e.chartId === chartId ? { ...e, ...points } : e
      ))))
      .catch(() => { chartsRequested.current.delete(chartId); });
  }, [activeId]);
  useEffect(() => {
    events.forEach((e) => {
      if (e.kind === 'chart.show' && e.lazy && !e.x && e.chartId && pinIds.includes(e.chartId)) loadChart(e.chartId);
    });
  }, [events, pinIds, loadChart]);
  const togglePin = (chartId: string) => {
    setPinIds((ids) => (ids.includes(chartId) ? ids.filter((i) => i !== chartId) : [...ids, chartId]));
  };
  const openJournal = (ids: number[]) => {
    setPanelOpen(true);
    setCommand({ tab: 'changes', journalIds: ids, nonce: Date.now() });
  };
  const showStep = (callId: string) => {
    if (narrow) setPanelOpenNarrow(false);
    setFocusStep({ callId, nonce: Date.now() });
  };
  useEffect(() => {
    if (!focusStep) return undefined;
    const timer = window.setTimeout(() => setFocusStep(null), 2600);
    return () => window.clearTimeout(timer);
  }, [focusStep]);

  const refreshRail = useCallback(async () => {
    const { groups: next } = await api.listConversations();
    setGroups(next);
  }, []);

  // The conversation's stored model must keep up with the transcript describing it.
  // Snapshotting used to be a button nobody had a reason to press, so a model the
  // agent built was lost the moment the session moved on. Captured here instead:
  // when a turn finishes and Ragnarok's change counter has moved past what this
  // conversation has saved, save it.
  const lastSavedVersion = useRef<number | null>(null);
  const autoSnapshot = useCallback(async (conversationId: string, version: number | null) => {
    if (version === null || lastSavedVersion.current === version) return;
    lastSavedVersion.current = version;
    try {
      await api.snapshot(conversationId);
      await refreshRail();
    } catch {
      /* best-effort: a failed save must not interrupt the conversation, and the
         next turn will try again. */
    }
  }, [refreshRail]);

  useEffect(() => {
    void (async () => {
      try {
        const [cfg, hp, eng] = await Promise.all([api.config(), api.health(), engineApi.get()]);
        setConfig(cfg);
        setHealth(hp);
        setEngineInfo(eng);
        // Open on setup until an engine is both chosen AND up.
        setShowSetup(!eng.configured || !eng.process.running);
        const rail = await api.listConversations();
        setGroups(rail.groups);
        // Resume where the user left off. Re-activating is what restores the model
        // AND the transcript together, so the conversation continues rather than
        // restarting beside a session whose contents nobody explained.
        if (rail.activeId) {
          const opened = await api.activate(rail.activeId);
          setActiveId(rail.activeId);
          setEvents(opened.events ?? []);
        }
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
        // `isWorkingState` mirrors the server's own definition. Testing for
        // `!== 'idle'` here left the composer disabled forever, because the last
        // state published at the end of a turn is `done` — `idle` is only ever set
        // internally, never sent.
        setBusy(isWorkingState((data as unknown as { state?: string }).state));
        return;
      }
      // A reconnecting stream replays what it missed, and a replayed chart would be
      // drawn twice under one id (and one React key). A chart id is unique per draw.
      setEvents((prev) => (
        data.kind === 'chart.show' && data.chartId && prev.some((e) => e.kind === 'chart.show' && e.chartId === data.chartId)
          ? prev : [...prev, data]
      ));
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

  // A turn has finished (busy fell) and the model moved — persist it.
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !busy && activeId) {
      void autoSnapshot(activeId, engineVersion);
    }
    wasBusy.current = busy;
  }, [busy, activeId, engineVersion, autoSnapshot]);

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

  /**
   * Send a turn, with any files the user attached.
   *
   * The agent gets the stored PATHS appended (that is the only way it learns a
   * file arrived — `read_attachment` takes a path); the transcript keeps the
   * user's own words plus chips, so the paths do not clutter what they wrote.
   */
  const send = async (message: string, files: UploadedFile[] = []) => {
    if (!config || !activeId) return;
    const forAgent = message + attachmentBlockFor(files);
    setEvents((prev) => [
      ...prev,
      { kind: 'chat.message', role: 'user', markdown: message, files },
    ]);
    setBusy(true);
    try {
      await api.chat(forAgent, activeId, config.sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The agent did not accept that message.');
      setBusy(false);
    }
  };

  /**
   * Follow the agent's pointer: switch to the Ragnarok tab and tell it what to
   * show. Ragnarok validates the origin and whitelists the tab on its side, so
   * this is a request, not a remote control.
   */
  const showInRagnarok = (event: ChatEvent) => {
    if (!config) return;
    setTab('ragnarok');
    const message = {
      type: 'ragnarok:show',
      tab: event.tab ?? 'Model',
      select: event.select ?? '',
      view: event.view ?? '',
    };
    // The iframe may still be mounting on first switch; a short retry costs
    // nothing and avoids a click that silently does nothing.
    const send = () => frameRef.current?.contentWindow?.postMessage(message, config.ragnarokBase);
    send();
    window.setTimeout(send, 400);
  };

  const upload = async (files: File[]) => {
    if (!config) throw new Error('Not connected yet.');
    const stored = await api.uploadFiles(files, config.sessionId, activeId);
    return stored.files;
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
        {railNarrow && tab === 'conversation' && (
          <button className="btn-ghost sm" aria-pressed={railOpen} onClick={() => setRailOpen((o) => !o)}>Conversations</button>
        )}
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
            {engineVersion !== null && (
              <span className="chip chip--muted"
                title="Ragnarok records every change to the model. This is how many it has recorded — it goes up whenever the agent (or you) change something.">
                {engineVersion} {engineVersion === 1 ? 'change' : 'changes'}
              </span>
            )}
          </button>
        </div>
        {tab === 'conversation' && (
          <button className={`btn-ghost sm state-toggle${panelOpen ? ' is-on' : ''}`} aria-pressed={panelOpen}
            onClick={() => setPanelOpen(!panelOpen)} title="Show or hide the live State panel">
            State
          </button>
        )}
        <span className="topbar-right muted">
          {config && (
            <AgentControls sessionId={config.sessionId} busy={busy} onError={setError} />
          )}
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

      <main className="body" ref={bodyRef}>
        <div className={`pane${tab === 'conversation' ? '' : ' is-hidden'}`}
          style={{ '--state-w': `${Math.min(panelWidth, maxPanel())}px` } as React.CSSProperties}>
          {(!railNarrow || railOpen) && (
          <ConversationRail
            overlay={railNarrow}
            groups={groups}
            activeId={activeId}
            engineVersion={engineVersion}
            busy={busy}
            onSelect={(id) => { setRailOpen(false); void select(id); }}
            onCreate={() => void create()}
            onRename={(c) => void rename(c)}
            onRegroup={(c) => void regroup(c)}
            onDelete={(c) => void remove(c)}
            onSnapshot={(c) => void snapshot(c)}
          />
          )}
          <ChatPane
            events={events}
            onNeedChart={loadChart}
            streaming={streaming}
            busy={busy}
            disabled={chatDisabled}
            disabledReason={chatDisabledReason}
            onSend={(m, files) => void send(m, files)}
            onUpload={upload}
            onAnswerQuestion={(id, answer) => {
              // The agent is BLOCKED until this lands, so clear the working state
              // optimistically — leaving the composer disabled after answering
              // reads as a hang.
              void api.answerQuestion(id, answer).catch((e) =>
                setError(e instanceof Error ? e.message : 'Could not send that answer.'));
            }}
            onShowInRagnarok={showInRagnarok}
            pinnedIds={new Set(pinIds)}
            onTogglePin={togglePin}
            onOpenJournal={openJournal}
            focusStep={focusStep}
            onDecideApproval={(id, decision) => {
              void api.decideApproval(id, decision).catch((e) =>
                setError(e instanceof Error ? e.message : 'Could not send that decision.'));
            }}
          />
          {panelOpen && !narrow && (
            <SplitPane width={Math.min(panelWidth, maxPanel())} min={PANEL_MIN_PX} max={maxPanel()}
              onChange={setPanelWidth} onReset={() => setPanelWidth(PANEL_DEFAULT_PX)} />
          )}
          {panelOpen && (
            <div className={`state-wrap${narrow ? ' is-overlay' : ''}`}>
              <StatePanel data={stateData} engineVersion={engineVersion} command={command}
                callOf={callOf} onFocusStep={showStep} pinned={pinned}
                onUnpin={togglePin} onCollapse={() => setPanelOpen(false)} />
            </div>
          )}
          {!panelOpen && !narrow && (
            <button className="state-handle" onClick={() => setPanelOpen(true)} title="Show the State panel">State</button>
          )}
        </div>

        {/* Mounted once, hidden on the other tab — remounting would reload
            Ragnarok and lose its scroll, open sheet and map viewport. */}
        <div className={`pane pane--frame${tab === 'ragnarok' ? '' : ' is-hidden'}`}>
          {config && (
            <iframe ref={frameRef} className="engine-frame" src={config.ragnarokBase} title="Ragnarok" />
          )}
        </div>
      </main>
    </div>
  );
}
