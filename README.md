# Bifrost

An AI-agentic power-modelling IDE. The agent does the modelling; Ragnarok is the engine.

## The split, and why it exists

Bifrost holds **no PyPSA, no solver, and no copy of the workbook.** Every modelling
capability — sheets, solves, results, imports, transforms, topology reduction — lives
in [Ragnarok](https://github.com/iam-hongsanghyun/project-bifrost) and is reached over
HTTP.

The previous incarnation was a fork of the whole engine: 136,000 lines carried so that
one chat panel could be added. The cost was not disk, it was correctness — the same bug
had to be found and fixed twice, in two places, by two people who did not know the other
was doing it. This repo exists so that cannot happen again.

What Bifrost owns:

| | |
|---|---|
| `bifrost/agent/` | A Claude Agent SDK session whose tools are Ragnarok's MCP surface |
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
    project.zip    Ragnarok's own format — model + data + result + transcript
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

- the agent edits via Ragnarok's API → journalled, tagged `actor=agent`
- the Ragnarok tab is the real Ragnarok UI, subscribed to that event stream
- `tool.end` events carry the journal entry ids a tool produced, so the chat can show
  what each step actually changed

## Layout

```
bifrost/          application code
  main.py         FastAPI: agent + conversations + config
  config.py       every tunable from the environment
  ragnarok.py     the HTTP client, with a transport seam for in-process tests
  agent/          Claude Agent SDK host (status, chat, approvals, questions, files…)
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
python -m pytest tests -q
```

`tests/test_roundtrip_against_ragnarok.py` drives Ragnarok's **real** app in-process over
an ASGI transport — its actual export/import endpoints and session store, no server to
start. It skips rather than fails when Ragnarok is not importable, so this suite stays
runnable alone. Point it with `RAGNAROK_ROOT` (default `~/github/ragnarok`).

## Choosing the engine

First run opens a setup gate rather than the chat — with no engine there is no model
to talk about, so offering a conversation first would offer one that cannot do
anything.

Bifrost probes the obvious places (a sibling `ragnarok/`, `~/github/ragnarok`) and
offers what it finds; otherwise the path is pasted. A directory is accepted only
when it really is a Ragnarok checkout — `backend/app/main.py` and
`backend/requirements.txt` must exist — and refusal says which marker was missing.
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

- `RAGNAROK_API_BASE` — where the engine is.
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
`127.0.0.1:8000` is a *different origin*, so the frame gets partitioned storage and
Ragnarok's boot, which reads `localStorage`, dies inside it.

The fix is same-origin, and it is not a one-line proxy: both apps serve their API at
`/api`, so putting Ragnarok behind a Bifrost path would send its own calls to
Bifrost. Either Bifrost's API moves to its own prefix, or Ragnarok is served on a
subpath with a matching API prefix. Now that Bifrost launches the engine it also
controls the port, which makes proxying it on one origin the natural next step.
