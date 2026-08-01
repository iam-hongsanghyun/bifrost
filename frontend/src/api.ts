/**
 * The Bifrost API, and the two event streams the shell listens to.
 *
 * There are deliberately two. Bifrost's stream carries the conversation — chat
 * deltas, tool start/end, approvals. Ragnarok's carries the model — a
 * `session.version` bump whenever anything mutates the session, by anyone. The
 * second is why the Ragnarok tab is always current without Bifrost forwarding
 * anything: the browser subscribes to the engine directly.
 */

export interface Conversation {
  id: string;
  title: string;
  group: string;
  createdAt: string;
  updatedAt: string;
  projectVersion: number | null;
  eventCount: number;
  hasProject: boolean;
}

export interface ConversationGroup {
  group: string;
  conversations: Conversation[];
}

/** A transcript entry. `kind` mirrors the agent service's event names. */
export interface ChatEvent {
  kind: string;
  conversationId?: string;
  messageId?: string;
  role?: 'user' | 'assistant';
  markdown?: string;
  text?: string;
  /** tool.start / tool.end */
  callId?: string;
  tool?: string;
  ok?: boolean;
  resultSummary?: string;
  /** Ragnarok journal entries a tool produced — what it actually changed. */
  journalEntryIds?: number[];
  /** question.request / question.resolved */
  questionId?: string;
  question?: string;
  options?: string[];
  answer?: string;
  /** The question wants a FILE (a profile, a cost table), not a choice. */
  needsFile?: boolean;
  /** chat.attachments — what the user handed over, rendered as chips. */
  files?: UploadedFile[];
  /** chart.show */
  chartId?: string;
  title?: string;
  chartKind?: string;
  x?: string[];
  series?: { name: string; values: number[]; axis?: string; kind?: string }[];
  unit?: string;
  /** Secondary axis label — price in $/MWh against dispatch in MW. */
  unitRight?: string;
  /** Stack the series: dispatch by carrier, capacity by technology. */
  stacked?: boolean;
  note?: string;
  /** assumption.recorded */
  id?: string;
  subject?: string;
  value?: string;
  basis?: string;
  source?: string;
  affects?: string;
  /** approval.request / approval.resolved */
  approvalId?: string;
  args?: Record<string, unknown>;
  decision?: string;
}

/** One stored attachment. `path` is what `read_attachment` takes. */
export interface UploadedFile {
  name: string;
  storedAs: string;
  path: string;
  size: number;
}

export interface ClientConfig {
  ragnarokBase: string;
  sessionId: string;
  /** False on first run: the shell opens on setup instead of the chat. */
  engineConfigured: boolean;
}

export interface Health {
  ok: boolean;
  sessionId: string;
  engine: { reachable: boolean; base: string; error?: string };
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw new Error((await resp.text()) || `HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

export const api = {
  config: () => fetch('/api/config').then(json<ClientConfig>),
  health: () => fetch('/api/health').then(json<Health>),

  listConversations: () =>
    fetch('/api/conversations').then(
      json<{ groups: ConversationGroup[]; defaultGroup: string; activeId: string | null }>,
    ),

  createConversation: (title: string, group: string) =>
    fetch('/api/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, group }),
    }).then(json<Conversation>),

  /** Switching conversation switches WORLDS: this loads that conversation's
   *  project package into the shared Ragnarok session. */
  activate: (id: string) =>
    fetch(`/api/conversations/${id}/activate`, { method: 'POST' }).then(
      json<Conversation & { events: ChatEvent[]; loaded: unknown; sessionMeta: Record<string, unknown> }>,
    ),

  update: (id: string, patch: { title?: string; group?: string }) =>
    fetch(`/api/conversations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then(json<Conversation>),

  remove: (id: string) =>
    fetch(`/api/conversations/${id}`, { method: 'DELETE' }).then(json<{ deleted: boolean }>),

  saveEvents: (id: string, events: ChatEvent[]) =>
    fetch(`/api/conversations/${id}/events`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
    }).then(json<Conversation>),

  /** Capture the live session into this conversation's package, so the stored
   *  world keeps up with the transcript describing it. */
  snapshot: (id: string) =>
    fetch(`/api/conversations/${id}/snapshot`, { method: 'POST' }).then(
      json<Conversation & { bytes: number }>,
    ),

  chat: (message: string, conversationId: string, sessionId: string) =>
    fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, conversationId, sessionId }),
    }).then(json<Record<string, unknown>>),

  agentStatus: () => fetch('/api/agent/status').then(json<Record<string, unknown>>),

  /**
   * Hand files to the agent — a wind profile, a load trace, a cost table.
   *
   * The backend has stored uploads and offered `read_attachment` from the start;
   * until now there was no way to reach it from the UI, so the agent had a tool
   * for data the user could not give it. Multipart, no Content-Type header: the
   * browser must set its own multipart boundary.
   */
  uploadFiles: (files: File[], sessionId: string) => {
    const body = new FormData();
    files.forEach((file) => body.append('files', file, file.name));
    return fetch(`/api/agent/files?session_id=${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      body,
    }).then(json<{ files: UploadedFile[] }>);
  },

  /** Answer a pending `ask_user`. Until this lands the agent is BLOCKED — it is
   *  awaiting input, so an unanswerable question is a dead conversation. */
  answerQuestion: (questionId: string, answer: string) =>
    fetch(`/api/agent/questions/${questionId}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    }).then(json<Record<string, unknown>>),

  /** Allow or deny a tool call that needs approval. Same deal: it is waiting. */
  decideApproval: (approvalId: string, decision: 'allow' | 'deny') =>
    fetch(`/api/agent/approvals/${approvalId}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
    }).then(json<Record<string, unknown>>),
};

/** Subscribe to Bifrost's conversation stream. Returns an unsubscribe. */
export function subscribeChat(
  sessionId: string,
  onEvent: (kind: string, data: ChatEvent) => void,
): () => void {
  const source = new EventSource(`/api/events?session_id=${encodeURIComponent(sessionId)}`);
  const kinds = [
    'agent.status', 'chat.delta', 'chat.message',
    'tool.start', 'tool.end',
    'approval.request', 'approval.resolved',
    'chart.show',
    'assumption.recorded',
    'question.request', 'question.resolved',
  ];
  const handlers = kinds.map((kind) => {
    const handler = (event: MessageEvent) => {
      try {
        onEvent(kind, { kind, ...JSON.parse(event.data) });
      } catch {
        /* a malformed frame must not kill the stream */
      }
    };
    source.addEventListener(kind, handler as EventListener);
    return [kind, handler] as const;
  });
  return () => {
    handlers.forEach(([kind, handler]) => source.removeEventListener(kind, handler as EventListener));
    source.close();
  };
}

/**
 * Subscribe to RAGNAROK's stream, straight from the engine.
 *
 * This is what makes the Ragnarok tab always-current: every session mutation —
 * the agent's, the user's in the embedded UI, an importer's — bumps
 * `session.version`, and Ragnarok publishes it because it journals at the store
 * facade rather than per-endpoint. Bifrost forwards nothing.
 */
export function subscribeEngine(
  ragnarokBase: string,
  sessionId: string,
  onVersion: (version: number | null) => void,
): () => void {
  const url = `${ragnarokBase.replace(/\/$/, '')}/api/events?session_id=${encodeURIComponent(sessionId)}`;
  const source = new EventSource(url);
  const handler = (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data) as { version?: number };
      onVersion(typeof data.version === 'number' ? data.version : null);
    } catch {
      onVersion(null);
    }
  };
  source.addEventListener('session.version', handler as EventListener);
  return () => {
    source.removeEventListener('session.version', handler as EventListener);
    source.close();
  };
}

// ── the engine: which Ragnarok, and is it up ────────────────────────────────

export interface EngineRootCheck {
  path: string;
  valid: boolean;
  reason: string;
  interpreter: string | null;
}

export interface EngineProcessStatus {
  /** 'bifrost' when we started it, 'external' when the user did, null when down. */
  owner: 'bifrost' | 'external' | null;
  running: boolean;
  port: number;
  root: string | null;
  logPath: string | null;
  exitCode: number | null;
}

export interface EngineInfo {
  configured: boolean;
  root: string | null;
  check: EngineRootCheck | null;
  apiBase: string;
  process: EngineProcessStatus;
  bifrostPython: string;
  suggestions: EngineRootCheck[];
}

export const engineApi = {
  get: () => fetch('/api/engine').then(json<EngineInfo>),
  check: (path: string) =>
    fetch('/api/engine/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    }).then(json<EngineRootCheck>),
  setRoot: (path: string) =>
    fetch('/api/engine/root', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    }).then(json<EngineInfo & { saved: boolean }>),
  start: () => fetch('/api/engine/start', { method: 'POST' })
    .then(json<{ started: boolean; reason: string } & EngineProcessStatus>),
  stop: () => fetch('/api/engine/stop', { method: 'POST' })
    .then(json<{ stopped: boolean; reason: string } & EngineProcessStatus>),
  log: (lines = 60) => fetch(`/api/engine/log?lines=${lines}`).then(json<{ lines: string[] }>),
};
