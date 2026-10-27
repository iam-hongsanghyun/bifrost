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
  /** A stored chart arrives as a stub (no `x` / `series`) until `api.chart` fills it. */
  lazy?: boolean;
  title?: string;
  chartKind?: string;
  x?: string[];
  series?: { name: string; values: (number | null)[]; axis?: string; kind?: string; color?: string }[];
  /** Set when the server thinned the chart: the number of points the agent sent. */
  thinnedFrom?: number;
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
  /** show.request — the agent pointing at a Ragnarok view */
  tab?: string;
  select?: string;
  view?: string;
  why?: string;
  /** approval.request / approval.resolved */
  approvalId?: string;
  args?: Record<string, unknown>;
  decision?: string;
}

/** A hydrated stored chart goes back to the server as the stub it came as: the
 *  points are already on disk, and re-sending megabytes every save helps no one. */
export function slimEvent(event: ChatEvent): ChatEvent {
  if (event.kind !== 'chart.show' || !event.lazy) return event;
  const { x: _x, series: _series, ...stub } = event;
  return stub;
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

/** What the agent is, and what it is doing right now. */
export interface AgentStatus {
  available: boolean;
  /** False until an engine checkout with its own venv is chosen: the agent then has no modelling tools. */
  toolsConfigured?: boolean;
  auth: string;
  state: string;
  autonomy: string;
  model: string | null;
  models: string[];
  effort: string;
  efforts: string[];
  costUsd: number;
}

export interface Health {
  ok: boolean;
  sessionId: string;
  engine: { reachable: boolean; base: string; error?: string };
}

/**
 * Unwrap a response, and unwrap its ERROR too.
 *
 * This used to throw the raw body, so a FastAPI failure reached the user as
 * `{"detail":"agent_busy: another conversation is running."}` — JSON in a banner,
 * which tells them nothing about what to do. FastAPI puts the human sentence in
 * `detail`; take it. Falls back to the raw text, then to the status code, because
 * an unparseable error still has to say something.
 */
async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const raw = await resp.text();
    let message = raw;
    try {
      const body = JSON.parse(raw) as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) {
        message = body.detail;
      } else if (Array.isArray(body.detail)) {
        // Pydantic validation errors: [{loc, msg, type}, …]
        message = body.detail
          .map((d) => (d as { msg?: string }).msg)
          .filter(Boolean)
          .join('; ');
      }
    } catch {
      /* not JSON — keep the raw text */
    }
    throw new Error(message.trim() || `HTTP ${resp.status}`);
  }
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
      body: JSON.stringify({ events: events.map(slimEvent) }),
    }).then(json<Conversation>),

  /** The points of one stored chart (its transcript entry is a `lazy` stub). */
  chart: (id: string, chartId: string) =>
    fetch(`/api/conversations/${id}/charts/${encodeURIComponent(chartId)}`).then(
      json<Pick<ChatEvent, 'x' | 'series'>>,
    ),

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

  agentStatus: (sessionId: string) =>
    fetch(`/api/agent/status?session_id=${encodeURIComponent(sessionId)}`).then(json<AgentStatus>),

  /** Which model runs the next turn. Takes effect at the start of a turn. */
  setModel: (model: string, sessionId: string) =>
    fetch('/api/agent/model', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, sessionId }),
    }).then(json<{ model: string | null; models: string[] }>),

  /** How hard it thinks. Cheap questions do not need 'high'; wrong answers cost more. */
  setEffort: (effort: string, sessionId: string) =>
    fetch('/api/agent/effort', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ effort, sessionId }),
    }).then(json<{ effort: string; efforts: string[] }>),

  /** Stop the current turn. Pending approvals deny, pending questions release. */
  interrupt: (sessionId: string) =>
    fetch('/api/agent/interrupt', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId }),
    }).then(json<{ interrupted: boolean }>),

  /**
   * Hand files to the agent — a wind profile, a load trace, a cost table.
   *
   * The backend has stored uploads and offered `read_attachment` from the start;
   * until now there was no way to reach it from the UI, so the agent had a tool
   * for data the user could not give it. Multipart, no Content-Type header: the
   * browser must set its own multipart boundary.
   */
  uploadFiles: (files: File[], sessionId: string, conversationId: string | null) => {
    const body = new FormData();
    files.forEach((file) => body.append('files', file, file.name));
    // Named conversation, not just session: the file is stored in that
    // conversation's directory so it travels with the project it belongs to.
    const q = new URLSearchParams({ session_id: sessionId });
    if (conversationId) q.set('conversation_id', conversationId);
    return fetch(`/api/agent/files?${q.toString()}`, { method: 'POST', body })
      .then(json<{ files: UploadedFile[] }>);
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
    'show.request',
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
  /** 'bifrost' when we started it, 'external' when the user did, 'foreign' when
   *  the port is held by a DIFFERENT Ragnarok checkout, null when down. */
  owner: 'bifrost' | 'external' | 'foreign' | null;
  /** Which checkout is squatting the port, when owner is 'foreign'. */
  foreignRoot?: string | null;
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

// ── the State panel: live model, journal, queue, runs, results ──────────────
// Every route is Bifrost's own (`/api/state/*`); the browser never calls the
// engine for these. Keys carry their units (`capacity_mw`) and match
// bifrost/state.py one for one.

export interface ListResponse<T> {
  items: T[];
  total: number;
}

export interface ModelCount {
  sheet: string;
  label: string;
  count: number;
  /** bus | branch (joins two buses) | attached (hangs off one bus) | other */
  role: 'bus' | 'branch' | 'attached' | 'other';
  columns: string[];
}

export interface ModelCarrier {
  name: string;
  nice_name: string;
  color: string;
  co2_emissions_t_per_mwh_th: number | null;
  asset_count: number;
}

export interface TopologyAsset {
  sheet: string;
  name: string;
  carrier: string;
  p_nom_mw: number | null;
}

export interface TopologyBus {
  name: string;
  x: number | null;
  y: number | null;
  v_nom_kv: number | null;
  carrier: string;
  assets: TopologyAsset[];
  assets_total: number;
}

export interface TopologyBranch {
  sheet: string;
  name: string;
  bus0: string;
  bus1: string;
  capacity_mw: number | null;
  carrier: string;
}

export interface StateModel {
  loaded: boolean;
  version: number | null;
  filename?: string;
  snapshot_count?: number | null;
  snapshot_start?: string | null;
  snapshot_end?: string | null;
  series_sheets?: { name: string; rows: number | null; columns: string[] }[];
  counts: ModelCount[];
  carriers: ModelCarrier[];
  topology: {
    buses: TopologyBus[];
    branches: TopologyBranch[];
    has_coordinates: boolean;
    unattached: number;
  };
}

export type CellValue = string | number | boolean | null;

export interface TablePage {
  sheet: string;
  kind: string;
  columns: string[];
  /** The first column plus those whose value differs between rows. */
  key_columns: string[];
  items: Record<string, CellValue>[];
  total: number;
  offset: number;
  limit: number;
}

export interface JournalSheetChange {
  name: string;
  kind: string;
  rowsBefore: number | null;
  rowsAfter: number | null;
  added?: boolean;
}

export interface JournalEntry {
  id: number;
  version: number;
  ts: string;
  actor: string;
  origin: { endpoint?: string; requestId?: string; conversationId?: string | null };
  kind: string;
  sheets: JournalSheetChange[];
  summary: string;
  undo?: { strategy: string };
  hasDetail: boolean;
}

export interface JournalPage extends ListResponse<JournalEntry> {
  version: number | null;
  next_before: number | null;
}

export interface JournalDiff {
  id: number;
  kind: string;
  summary: string;
  detail: {
    cellDiffs?: { row: number; column: string; before: CellValue; after: CellValue }[];
    truncated?: boolean;
    [key: string]: unknown;
  };
  undoStrategy?: string;
}

export interface QueueJob {
  id: string;
  label: string;
  status: 'queued' | 'staged' | 'running' | 'done' | 'error' | 'cancelled';
  submittedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  runName: string | null;
  cores: number | null;
  elapsed_s: number | null;
}

export interface QueueState extends ListResponse<QueueJob> {
  active: number;
  concurrency: number | null;
  max_concurrency: number | null;
}

export interface RunMeta {
  name: string;
  label: string;
  saved_at: string | null;
  origin: string;
  filename: string;
  snapshot_start: string | null;
  snapshot_end: string | null;
  component_counts: Record<string, number>;
  summary: { label: string; value: string }[];
  size_bytes: number | null;
}

export interface Kpi {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  text: string | null;
}

export interface NamedSeries {
  name: string;
  values: (number | null)[];
  color?: string;
}

export interface CategoryChart {
  categories: string[];
  series: NamedSeries[];
}

export interface Dashboard {
  run: string;
  label: string;
  saved_at: string | null;
  origin: string;
  kpis: Kpi[];
  summary: { label: string; value: string; detail?: string }[];
  snapshot_count: number;
  stride: number;
  periods: string[];
  buses: string[];
  carriers: string[];
  selected: { bus: string | null; carrier: string | null; period: string | null };
  x: string[];
  /** Index of each x in the run's full snapshot order (the drill-in key). */
  snapshot_index: number[];
  dispatch_mw: { series: NamedSeries[]; load_mw: (number | null)[] | null };
  price: { series: NamedSeries[]; currency: string };
  curtailment_mw: { series: NamedSeries[] };
  emissions_tco2e: { values: (number | null)[] };
  storage: {
    soc_mwh: NamedSeries[];
    flow_mw: { charge: (number | null)[]; discharge: (number | null)[] } | null;
  };
  capacity_mw: CategoryChart;
  expansion: { name: string; component: string; carrier: string; bus: string;
               p_nom_mw: number; p_nom_opt_mw: number; delta_mw: number }[];
  emissions_by_carrier: CategoryChart;
  cost_breakdown: { label: string; value: number | null }[];
  carrier_mix: { name: string; energy_mwh: number | null; color?: string }[];
  line_loading: { name: string; loading_pct: number | null }[];
  bus_filter_note: string | null;
}

export interface SnapshotDrill {
  index: number;
  label: string;
  bus: string | null;
  by_carrier: { name: string; p_mw: number; color?: string }[];
  by_asset: { name: string; carrier: string; bus: string; p_mw: number }[];
  by_bus: { bus: string; price: number | null; generation_mw: number | null }[];
  load_mw: number | null;
  flows: { name: string; p0_mw: number | null }[];
}

export interface Comparison {
  runs: { name: string; label: string; saved_at: string | null }[];
  kpis: { key: string; label: string; unit: string; values: (number | null)[];
          text: (string | null)[]; delta: (number | null)[]; delta_pct: (number | null)[] }[];
  generation_mwh: CategoryChart;
  capacity_mw: CategoryChart;
  cost: CategoryChart;
  price: { name: string; x: string[]; values: (number | null)[] }[];
}

const jsonBody = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const qs = (params: Record<string, string | number | boolean | null | undefined>): string => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') q.set(k, String(v));
  });
  const text = q.toString();
  return text ? `?${text}` : '';
};

export const stateApi = {
  model: () => fetch('/api/state/model').then(json<StateModel>),
  sheet: (name: string, p: { q?: string; sort?: string; desc?: boolean; offset?: number; limit?: number }) =>
    fetch(`/api/state/sheet/${encodeURIComponent(name)}${qs(p)}`).then(json<TablePage>),
  journal: (limit = 100, before?: number) =>
    fetch(`/api/state/journal${qs({ limit, before })}`).then(json<JournalPage>),
  journalDiff: (id: number) => fetch(`/api/state/journal/${id}/diff`).then(json<JournalDiff>),
  queue: () => fetch('/api/state/queue').then(json<QueueState>),
  solve: (scenario: Record<string, unknown> = {}, options: Record<string, unknown> = {}) =>
    fetch('/api/state/queue', jsonBody({ scenario, options })).then(
      json<{ id: string; status: string; position: number }>),
  cancel: (id: string) =>
    fetch(`/api/state/queue/${encodeURIComponent(id)}/cancel`, { method: 'POST' }).then(json<Record<string, unknown>>),
  runs: () => fetch('/api/state/runs').then(json<ListResponse<RunMeta>>),
  dashboard: (run: string, p: { bus?: string | null; carrier?: string | null; period?: string | null }) =>
    fetch(`/api/state/runs/${encodeURIComponent(run)}/dashboard${qs(p)}`).then(json<Dashboard>),
  snapshot: (run: string, index: number, bus?: string | null) =>
    fetch(`/api/state/runs/${encodeURIComponent(run)}/snapshot${qs({ index, bus })}`).then(json<SnapshotDrill>),
  compare: (runs: string[]) =>
    fetch(`/api/state/compare${qs({ runs: runs.join(',') })}`).then(json<Comparison>),
};
