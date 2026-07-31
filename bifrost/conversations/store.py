"""Conversations, groups, and the project package each conversation owns.

The rule that shapes this module: **a conversation and the model it produced are
one artefact.** Reopening a conversation must put back the model, the data, the
results and the chat exactly as they were, or the transcript is describing a world
that no longer exists — an agent's "I set the carbon price to 80" is a lie if
reopening the chat shows a different model.

So a conversation is a directory:

    <data>/conversations/<id>/
        meta.json      group, title, timestamps, the Ragnarok journal version
                       the project was captured at
        project.zip    Ragnarok project package — canonical model JSON + meta +
                       conversation sidecar + readable xlsx
        events.json    the chat transcript (also inside project.zip; this is the
                       fast path so listing does not unzip)

``project.zip`` is Ragnarok's own format, written by its ``/api/export/project``
and read by ``/api/import/project/load``. Bifrost does not define a project
format, parse a workbook, or hold a second copy of the model — a conversation
carries an opaque blob it got from the engine and hands back.

The transcript is stored twice on purpose. Inside the package it travels with the
model (so a package handed to someone else is complete); alongside it as
``events.json`` so the rail can render titles and counts without unzipping every
conversation on every list call.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import CONFIG

logger = logging.getLogger("bifrost.conversations")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROJECT_NAME = "project.zip"
_META_NAME = "meta.json"
_EVENTS_NAME = "events.json"

#: Group a conversation lands in when the user has not filed it. A real name
#: rather than ``None`` so the rail needs no special case for "loose" chats.
DEFAULT_GROUP = "Unfiled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


@dataclass
class Conversation:
    """One chat and the model it produced."""

    id: str
    title: str
    group: str = DEFAULT_GROUP
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    #: Ragnarok journal version the stored package was captured at. Lets the UI
    #: say "the model has moved on since this was saved" instead of guessing.
    project_version: int | None = None
    event_count: int = 0

    def to_dict(self, *, has_project: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "group": self.group,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "projectVersion": self.project_version,
            "eventCount": self.event_count,
            "hasProject": has_project,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Conversation":
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or "Untitled"),
            group=str(raw.get("group") or DEFAULT_GROUP),
            created_at=str(raw.get("createdAt") or _now()),
            updated_at=str(raw.get("updatedAt") or _now()),
            project_version=raw.get("projectVersion"),
            event_count=int(raw.get("eventCount") or 0),
        )


class ConversationStore:
    """Filesystem-backed. One directory per conversation, no database.

    A conversation's package can be hundreds of MB for a full-year model, so
    nothing here loads a package into memory to answer a question about it — the
    listing path reads only ``meta.json``.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or CONFIG.conversations_dir

    # ── paths ─────────────────────────────────────────────────────────────────
    def _dir(self, conversation_id: str) -> Path:
        if not _safe_id(conversation_id):
            raise ValueError(f"Unsafe conversation id: {conversation_id!r}")
        return self.root / conversation_id

    def project_path(self, conversation_id: str) -> Path:
        return self._dir(conversation_id) / _PROJECT_NAME

    def has_project(self, conversation_id: str) -> bool:
        path = self.project_path(conversation_id)
        return path.exists() and path.stat().st_size > 0

    # ── read ──────────────────────────────────────────────────────────────────
    def get(self, conversation_id: str) -> Conversation | None:
        meta = self._dir(conversation_id) / _META_NAME
        if not meta.exists():
            return None
        try:
            return Conversation.from_dict(json.loads(meta.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            logger.exception("Unreadable conversation meta: %s", meta)
            return None

    def list(self) -> list[Conversation]:
        """Newest-updated first. Unreadable directories are skipped, not fatal —
        one corrupt conversation must not take the whole rail down."""
        if not self.root.exists():
            return []
        out: list[Conversation] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            found = self.get(child.name)
            if found is not None:
                out.append(found)
        out.sort(key=lambda c: c.updated_at, reverse=True)
        return out

    def grouped(self) -> list[dict[str, Any]]:
        """Conversations bucketed by group, for the left rail.

        Groups are ordered by their most recently touched conversation, so the
        one being worked in rises to the top. ``DEFAULT_GROUP`` sinks to the
        bottom regardless — an unfiled pile should not outrank named work.
        """
        buckets: dict[str, list[Conversation]] = {}
        for conversation in self.list():
            buckets.setdefault(conversation.group, []).append(conversation)
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (kv[0] == DEFAULT_GROUP, -_max_updated(kv[1])),
        )
        return [
            {
                "group": group,
                "conversations": [
                    c.to_dict(has_project=self.has_project(c.id)) for c in items
                ],
            }
            for group, items in ordered
        ]

    def events(self, conversation_id: str) -> list[dict[str, Any]]:
        path = self._dir(conversation_id) / _EVENTS_NAME
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Unreadable transcript: %s", path)
            return []
        return raw if isinstance(raw, list) else []

    def read_project(self, conversation_id: str) -> bytes | None:
        path = self.project_path(conversation_id)
        return path.read_bytes() if self.has_project(conversation_id) else None

    # ── write ─────────────────────────────────────────────────────────────────
    def create(self, *, title: str = "New conversation", group: str = DEFAULT_GROUP) -> Conversation:
        conversation = Conversation(id=uuid.uuid4().hex, title=title.strip() or "New conversation",
                                   group=group.strip() or DEFAULT_GROUP)
        self._dir(conversation.id).mkdir(parents=True, exist_ok=True)
        self._write_meta(conversation)
        return conversation

    def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        group: str | None = None,
    ) -> Conversation | None:
        conversation = self.get(conversation_id)
        if conversation is None:
            return None
        if title is not None and title.strip():
            conversation.title = title.strip()
        if group is not None:
            conversation.group = group.strip() or DEFAULT_GROUP
        conversation.updated_at = _now()
        self._write_meta(conversation)
        return conversation

    def save_events(self, conversation_id: str, events: list[dict[str, Any]]) -> Conversation | None:
        """Persist the transcript and bump ``updated_at``."""
        conversation = self.get(conversation_id)
        if conversation is None:
            return None
        self._write_json(self._dir(conversation_id) / _EVENTS_NAME, events)
        conversation.event_count = len(events)
        conversation.updated_at = _now()
        self._write_meta(conversation)
        return conversation

    def save_project(
        self, conversation_id: str, data: bytes, *, version: int | None = None
    ) -> Conversation | None:
        """Attach a Ragnarok project package to this conversation.

        Written to a temp file and moved into place, so a crash mid-write cannot
        leave a truncated package that would fail to import later — the one
        artefact whose loss would cost the user their model.
        """
        conversation = self.get(conversation_id)
        if conversation is None:
            return None
        target = self.project_path(conversation_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".zip.part")
        tmp.write_bytes(data)
        tmp.replace(target)
        conversation.project_version = version
        conversation.updated_at = _now()
        self._write_meta(conversation)
        logger.info(
            "Conversation %s: stored project package (%d bytes, version %s)",
            conversation_id, len(data), version,
        )
        return conversation

    def delete(self, conversation_id: str) -> bool:
        directory = self._dir(conversation_id)
        if not directory.exists():
            return False
        shutil.rmtree(directory)
        return True

    # ── internals ─────────────────────────────────────────────────────────────
    def _write_meta(self, conversation: Conversation) -> None:
        self._write_json(self._dir(conversation.id) / _META_NAME, conversation.to_dict())

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _max_updated(items: list[Conversation]) -> float:
    """Sort key helper: the group's most recent activity as a comparable float."""
    best = 0.0
    for item in items:
        try:
            best = max(best, datetime.fromisoformat(item.updated_at).timestamp())
        except ValueError:
            continue
    return best


STORE = ConversationStore()
