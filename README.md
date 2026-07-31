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

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn bifrost.main:app --port 8100 --reload
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

## Configuration

Copy `.env.example`. The two that matter:

- `RAGNAROK_API_BASE` — where the engine is.
- `RAGNAROK_SESSION_ID` — the session the agent and the embedded UI **both** act on.
  They must match, or the user watches a different model than the agent edits.

## Status

Working and tested: the conversation store, the project-package binding, the Ragnarok
client, the ported agent service, and the round-trip proof that reopening a conversation
restores its model — topology, time-series and time axis.

Not built yet: the frontend shell. Its design is settled — a top-level tab bar toggling
**Conversation** and **Ragnarok**, a left rail listing conversations grouped and
reorderable, a Claude-Desktop-style transcript in the centre, and the Ragnarok tab
embedding the real Ragnarok UI so the user confirms the work in the actual application
rather than a reimplementation of it.
