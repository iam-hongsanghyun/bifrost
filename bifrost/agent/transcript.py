"""Persisted conversation transcripts (append-only JSONL per conversation).

Same pattern as the provenance log: best-effort, one JSON line per chat event
(user message, assistant message, tool call/result, status). Lets the Assistant
tab re-render history after a page reload, and gives an auditable record of
what the agent said alongside the journal's record of what it did.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("pypsa_gui.agent.transcript")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _dir(session_id: str) -> Path:
    root = Path(os.environ.get("RAGNAROK_AGENT_TRANSCRIPT_DIR", str(_DATA_DIR / "agent")))
    return root / session_id


def _path(session_id: str, conversation_id: str) -> Path:
    return _dir(session_id) / f"{conversation_id}.jsonl"


def append(session_id: str, conversation_id: str, event: str, data: dict[str, Any]) -> None:
    try:
        path = _path(session_id, conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("agent transcript write failed (best-effort)")


def write_events(session_id: str, conversation_id: str, events: list[dict[str, Any]]) -> None:
    """Write a full event list as a conversation transcript (import path).

    Overwrites any existing file for ``conversation_id`` — callers import into
    a freshly minted conversation id, so this never clobbers live history.
    """
    path = _path(session_id, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def delete(session_id: str, conversation_id: str) -> bool:
    """Delete one conversation's transcript file (explicit Clear)."""
    try:
        _path(session_id, conversation_id).unlink()
        return True
    except OSError:
        return False


def read_state(session_id: str) -> dict[str, Any]:
    """Persisted session-level agent state ({conversationId, model, autonomy}).

    Lets a backend restart resume the same conversation and model instead of
    silently starting a fresh one. Empty dict when absent/corrupt.
    """
    try:
        data = json.loads((_dir(session_id) / "state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(session_id: str, state: dict[str, Any]) -> None:
    try:
        d = _dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except OSError:
        logger.warning("agent state write failed (best-effort)")


def read(session_id: str, conversation_id: str) -> list[dict[str, Any]]:
    try:
        lines = _path(session_id, conversation_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def list_conversations(session_id: str) -> list[dict[str, Any]]:
    """Conversations for a session, newest first (id + last-modified)."""
    d = _dir(session_id)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.jsonl"):
        out.append({"conversationId": p.stem, "modifiedAt": p.stat().st_mtime})
    out.sort(key=lambda e: -e["modifiedAt"])
    return out
