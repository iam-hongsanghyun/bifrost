# Bifrost

An AI-agentic power-modelling IDE. The agent does the modelling; Ragnarok is the engine.

## The split, and why it exists

Bifrost holds **no PyPSA, no solver, and no copy of the workbook.** Every modelling
capability — sheets, solves, results, imports, transforms, topology reduction — lives
in [Ragnarok](https://github.com/iam-hongsanghyun/project-Ragnarok) and is reached over
HTTP.

The previous incarnation was a fork of the whole engine: 136,000 lines carried so that
one chat panel could be added. The cost was not disk, it was correctness — the same bug
had to be found and fixed twice, in two places, by two people who did not know the other
was doing it. This repo exists so that cannot happen again.

What Bifrost owns:

| | |
|---|---|
| `bifrost/agent/` | A Claude Agent SDK session whose tools are Ragnarok's MCP surface |
| `bifrost/dataask.py` | `request_data`: validates rows the user supplies against Ragnarok's live schema (pure) |
| `bifrost/conversations/` | The left rail, and the project package each conversation owns |
| `bifrost/ragnarok.py` | The only route to a model |
| `frontend/` | The shell: conversation tab, Ragnarok tab, grouped rail |

The package is `bifrost/`, deliberately not `backend/app/` — Ragnarok uses that path, and
two checkouts sharing a package name cannot be imported into one process. The integration
test needs exactly that.

## A conversation is a world

The rule everything else follows: **a conversation and the model it produced are one
artefact.** A transcript saying *"I set the carbon price to 80"* is a lie if reopening the
chat shows a different model.

So each conversation is a directory holding a Ragnarok **project package** — the `.zip`
Ragnarok already writes, carrying the canonical model JSON, a meta sidecar, the chat
transcript, and a readable xlsx:

```
backend/data/conversations/<id>/
    meta.json      group, title, timestamps, the journal version it was captured at
    project.zip    Ragnarok's own format — model + data + result (+ a transcript sidecar if the engine wrote one)
    events.json    the transcript again, so listing the rail needs no unzip
```

Bifrost does not define a project format or parse a workbook. It stores an opaque blob it
got from the engine and hands it back. Consequences worth knowing:

- **Portable.** `GET /api/conversations/{id}/project` gives you a package that opens in
  Ragnarok directly. No Bifrost needed to read a Bifrost export.
- **Switching conversations switches worlds.** `POST /{id}/activate` loads that
  conversation's package into the shared Ragnarok session. A conversation with no package
  yet **clears** the session rather than leaving it — otherwise a fresh chat silently
  inherits the previous model and the agent reasons about something the user never
  mentioned, which is the worst kind of wrong because everything it says is internally
  consistent.
- **Snapshots keep up.** `POST /{id}/snapshot` captures the live session after the agent
  changes something, recording Ragnarok's journal `version` so the UI can tell a stale
  package from a current one instead of guessing.

## "Ragnarok captures every change in the conversation"

This needs no cooperation from Bifrost. Both surfaces point at **one Ragnarok session**,
and Ragnarok journals every mutation at its store facade — whoever made it, through
whatever route — then publishes a `session.version` event. So:

- the agent edits via Ragnarok's API → journalled, tagged `actor=agent` and with the conversation id
- the Ragnarok tab is the real Ragnarok UI, subscribed to that event stream
- `tool.end` events carry the journal entry ids a tool produced, so the chat can show
  what each step actually changed

## The State panel

Beside the chat (a draggable, collapsible split; a drawer on narrow screens) is a live
view of what the agent is working on, so you do not have to ask it what state things are in:

| Section | Shows | Live because |
|---|---|---|
| Model | component counts (click for the sortable, filterable table), carriers, a clickable network map (click a bus for its assets, an asset for its row) | Ragnarok's `session.version` event refetches it |
| Changes | the journal as a timeline with cell-level diffs; each change links to the chat step that made it (`tool.end` carries the journal ids) and the chat links back | same |
| Runs | the solve queue (status and a live timer; Ragnarok reports no percentage, so none is invented), stored runs, a Solve button, pick up to five to compare | the queue is polled: 1.5 s while a job is active, 6 s otherwise, never while the tab is hidden |
| Results | KPIs, dispatch stack, prices by bus, price heatmap, capacity, storage, curtailment, emissions, cost; bus / carrier / period selectors; click a time point for that snapshot's dispatch; compare runs | a finished solve is selected automatically |
| Pinned | charts pinned from the transcript | stored as chart ids in `localStorage` (`bifrost.pins`, versioned) |

The browser never calls Ragnarok for any of this: every read goes through `/api/state/*`
(`bifrost/state_router.py`), which does one or two fetches and hands the JSON to
`bifrost/state.py` to shape (columns for charts, numbers for KPIs, `{items, total}` for lists).
The routes: `GET /model`, `/sheet/{name}`, `/journal`, `/journal/{id}/diff`, `/queue`, `/runs`,
`/runs/{run}/dashboard`, `/runs/{run}/snapshot`, `/compare`; `POST /queue`, `/queue/{id}/cancel`.

### Charts

One component (`frontend/src/components/Chart.tsx`, logic in `lib/chartModel.ts`) draws every
chart, in the transcript, the panel and full screen: hover tooltip with every series at the
hovered x, wheel / slider / box zoom with reset, legend click to hide and double-click to
isolate, line / area / stacked area / bar / stacked bar, duration curve, log y, full screen,
PNG and CSV export, click a point to break it down, pin to the panel. It resizes with its
container and follows the page's theme. The agent's `show_chart` payload is shaped by
`bifrost/charts.py`: a missing value is a gap that keeps its place, a year of hourly points
is no longer cut to 2,000, and only past `BIFROST_CHART_MAX_CELLS` is a chart thinned, with
indices shared by every series so peaks survive and stacks still add up.

Tunables (environment): `BIFROST_CHART_MAX_POINTS` (10000), `BIFROST_CHART_MAX_CELLS`
(250000, the most the browser draws), `BIFROST_TABLE_MAX_ROWS` (20000).

## Asking the user for data (`request_data`)

When a gap cannot be filled from open data — a private fleet, a confidential topology —
the agent does not invent defaults. It calls the in-process tool
`mcp__bifrost__request_data(component, columns, reason, example)` and the chat shows a
**form card**:

- the target sheet and the columns needed (the schema's required ones plus what the agent
  named), with type and unit, read from Ragnarok's live `/api/config` schema, never a copy;
- three ways in, one path out: **paste CSV** (a spreadsheet selection, tab- or
  semicolon-separated, works), **choose or drop a `.csv` file**, or a small **form grid**;
- validation as you type, shown as row-level errors (line of the paste, column, message):
  required and unknown columns (an unknown column is an error, so a typo does not silently
  drop data), types, units (a header may say `p_nom [kW]`; known power / energy / voltage /
  per-unit conversions apply, anything else is refused), `bus` / `bus0` / `bus1` must exist
  in the model (with a "did you mean"), duplicate names. Warnings, which do not block: a new
  carrier (added to `carriers`), a name that exists (Ragnarok's merge saves it as `name_2`).

**Add rows to the model** merges only when every row is valid, through Ragnarok's
`POST /api/session/merge-fragment` (`apply`), as the *user*: the journal entry is tagged
`user` with this conversation's id, and there is **no second approval**, because the person
typed those rows into a card the agent put in front of them. The agent gets back a summary
(rows, columns, renamed names, carriers added, journal entry ids). **Cancel** tells the agent
not to invent the rows; no answer within `BIFROST_DATAASK_TIMEOUT_S` says the same. A time
series is still `ask_user` with `needsFile`; `request_data` is for rows of a component sheet.

Routes (`/api/agent/data-requests/{id}`): `POST /validate` (changes nothing), `POST /submit`,
`POST /cancel`; `GET /api/agent/status` lists `pendingDataRequests`. Limits:
`BIFROST_DATAASK_MAX_ROWS` (5000), `BIFROST_DATAASK_TIMEOUT_S` (900). Tests:
`tests/test_dataask.py` (validation), `tests/test_datarequests.py` (lifecycle against a mock
engine), `tests/test_dataask_roundtrip.py` (against the real Ragnarok: its schema, its
merge, its journal).

## Layout

```
bifrost/          application code
  main.py         FastAPI: agent + conversations + config
  config.py       every tunable from the environment
  ragnarok.py     the HTTP client, with a transport seam for in-process tests
  state.py        shapes Ragnarok's model / journal / runs for the State panel (pure)
  state_router.py /api/state/*: thin fetches, then state.py
  charts.py       show_chart arguments -> chart.show payload (pure)
  dataask.py      validate user-supplied rows against Ragnarok's schema (pure)
  agent/          Claude Agent SDK host (status, chat, approvals, questions, data requests, files…)
  conversations/  store + router
tests/            mirrors bifrost/
frontend/         the shell
```

## Running it

Double-click **`bifrost.command`**. It builds the Python environment and the
interface dependencies on first run, starts both halves, and opens the browser. It
does *not* start Ragnarok — Bifrost asks which Ragnarok checkout to use, remembers
it, and launches it from the app, so the launcher only ever brings up Bifrost.

The window stays open while Bifrost runs; closing it stops both halves. Failures
print what to do about them rather than vanishing.

By hand:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn bifrost.main:app --port 8770 --reload
cd frontend && npm install && npm run dev
```

The agent SDK is optional (`requirements-agent.txt`), like Ragnarok's MCP extra:
`GET /api/agent/status` reports a degraded state when it is missing, so the app runs and
the Ragnarok tab works without it.

Tests:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

`tests/test_roundtrip_against_ragnarok.py` drives Ragnarok's **real** app in-process over
an ASGI transport — its actual export/import/session/journal endpoints, its session store
and its MCP server's confirm gate — with no server to start. It skips rather than fails
when Ragnarok is not importable, so the suite stays runnable alone. Ragnarok needs
PyPSA, pandas and the rest to import, which Bifrost's venv deliberately lacks, so to run it
(it is skipped in the line above) use the engine checkout's own interpreter:

```bash
~/github/ragnarok/.venv-pypsa/bin/python -m pytest tests -q -p no:cacheprovider
```

Point it at another checkout with `RAGNAROK_ROOT` (default `~/github/ragnarok`). The same
command runs `tests/test_policy.py`'s live half, which asks the checkout for its real tool
catalogue and fails when the approval policy has fallen behind it.

## The agent's tools and who approves them

The agent's whole toolset is Ragnarok's MCP server (`python -m backend.mcp`, 189 tools at
the time of writing), launched by the SDK from the **chosen checkout, on that checkout's own
interpreter**. Two gates exist for a mutating tool, and they must not stack:

- Ragnarok's own: a mutating tool returns a *preview* unless called with `confirm=true`,
  depending on `RAGNAROK_MCP_AUTONOMY`.
- Bifrost's: the SDK's `can_use_tool` callback raises an approval card
  (`bifrost/agent/policy.py`), by autonomy level `manual` / `guided` / `auto` / `bypass`.

Bifrost launches the MCP server with `RAGNAROK_MCP_AUTONOMY=auto`, which turns Ragnarok's gate
**off**, so an approved call runs once instead of coming back as a preview. The policy is
therefore the only gate, and it classifies every tool by name: read-only tools (including
the whole results/statistics/comparison surface) always flow; file exports flow while they
write inside the agent's working directory; cheap edits flow under `guided`; imports,
transforms and model swaps ask under `guided`; solves, deletions, anything that installs or
runs code, and stored credentials ask under `auto`. A tool the policy has never heard of asks
at every level but `bypass`. `GET /api/agent/tools` lists every tool with the levels that ask
about it, and `?live=true` compares it with the checkout's real catalogue.

## Ragnarok routes Bifrost uses

| Route | For |
|---|---|
| `GET /api/status`, `GET /api/identity` | readiness; refusing a different checkout on the port |
| `GET /api/session/meta`, `POST /api/session/model`, `POST /api/session/clear?session_id=` | the shared session |
| `GET /api/session/journal` | which journal entries a tool call produced |
| `POST /api/export/project`, `POST /api/import/project/load` | a conversation's package |
| `GET /api/events` | Ragnarok's `session.version` stream, read by the browser directly |
| `GET /api/config`, `POST /api/session/merge-fragment` | `request_data`: the live schema to validate against, and the merge |

`/api/import/project/load` only *parses* a package; Bifrost then puts the model into the
session with `POST /api/session/model`. The tools themselves reach Ragnarok through the MCP
server, not through Bifrost.

## Choosing the engine

First run opens a setup gate rather than the chat — with no engine there is no model
to talk about, so offering a conversation first would offer one that cannot do
anything.

Bifrost probes the obvious places (a sibling `ragnarok/`, `~/github/ragnarok`) and
offers what it finds; otherwise the path is pasted. A directory is accepted only
when it really is a Ragnarok checkout — `backend/app/main.py` and
`backend/requirements.txt` must exist — and refusal says which marker was missing.
A checkout without `backend/mcp/` is accepted but flagged: the engine would run and the
agent would have no tools.
Accepting anything and failing later at the first API call would report the problem
far from the choice that caused it.

Bifrost starts the engine with the checkout's own `.venv-pypsa`, never the system
interpreter: PyPSA, linopy and HiGHS are not there, so it would fail at import
inside a subprocess nobody is watching. If the checkout has no environment yet,
Bifrost says to run Ragnarok's own `run.command` once and stops rather than trying.

Two behaviours worth knowing:

- **It will not start a second engine.** If something already answers on the port,
  Bifrost adopts it and labels it *started outside Bifrost*. Two engines writing one
  session would interleave changes to the same model.
- **It only stops what it started.** An engine you launched yourself is left alone;
  closing it could discard work happening outside this app.

The engine's own output is tailed into the setup screen, so a failed start is
diagnosable there instead of only in a terminal you never opened.

## Configuration

Copy `.env.example`. The two that matter:

- `RAGNAROK_API_BASE` — where the engine is (default `http://127.0.0.1:8772`; the agent's MCP
  subprocess uses the same base unless `RAGNAROK_AGENT_API_BASE` says otherwise).
- `RAGNAROK_SESSION_ID` — the session the agent and the embedded UI **both** act on.
  They must match, or the user watches a different model than the agent edits.

Bifrost serves its API on **8770** and its interface on **8771** — chosen to collide
with nothing: not Vite's 5173, not Ragnarok's 8000, and outside the usual dev
defaults. Override with `BIFROST_API_PORT` / `BIFROST_UI_PORT`; the Vite proxy target
is derived from the first, so both halves move together.

## Status

Working and tested: the conversation store, the project-package binding, the Ragnarok
client, the ported agent service, and the round-trip proof that reopening a conversation
restores its model — topology, time-series and time axis.

Working: the launcher, the engine picker and supervisor, the conversation rail with
groups, activation (switching conversation switches the model), the transcript and
composer, and both event streams — Bifrost's for the chat and Ragnarok's
`session.version` straight from the engine.

**Known broken: the Ragnarok tab renders blank.** Not a layout bug — the iframe is
present, sized and visible, and its `GET /` returns 200; Ragnarok is fine opened
directly. The cause is that the UI on `localhost:8771` embedding the engine on
`127.0.0.1:8772` is a *different origin*, so the frame gets partitioned storage and
Ragnarok's boot, which reads `localStorage`, dies inside it.

The fix is same-origin, and it is not a one-line proxy: both apps serve their API at
`/api`, so putting Ragnarok behind a Bifrost path would send its own calls to
Bifrost. Either Bifrost's API moves to its own prefix, or Ragnarok is served on a
subpath with a matching API prefix. Now that Bifrost launches the engine it also
controls the port, which makes proxying it on one origin the natural next step.
