"""Every number the agent did not get from data.

The gap this closes, in the agent's own words from a real run: *"5% is a harmless
placeholder rather than a study assumption."* It was honest and it was correct — and
that value then sat inside a solved model with nothing marking it as invented.

A modelling tool whose figures cannot be traced is a demo. So any number the agent
supplies itself is recorded here with its basis, kept in the conversation's own
directory so it travels with the project package, and surfaced in the transcript. Then
"where did 5% come from?" has an answer that does not depend on re-reading the chat.

What this is NOT: a validator. It does not judge whether an assumption is reasonable —
that is the modeller's call and the whole point of showing them. It only guarantees
the assumption is *visible* rather than buried.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .conversations.store import STORE

logger = logging.getLogger("bifrost.assumptions")

_FILE = "assumptions.json"

#: Where a number came from. `assumed` is the one that matters — it is the agent's
#: own choice and the reason this module exists. The others are recorded so a
#: register read end-to-end shows the whole provenance chain, not only its weakest
#: links.
BASES = ("assumed", "user", "dataset", "default")


@dataclass
class Assumption:
    """One number, and why it has that value."""

    id: str
    subject: str          # what it applies to: "discount rate", "gas marginal cost"
    value: str            # kept as text: units, ranges and "n/a" are all legitimate
    unit: str = ""
    basis: str = "assumed"
    source: str = ""      # dataset name, the user's words, or the reasoning
    affects: str = ""     # what in the model or the result depends on it
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "subject": self.subject, "value": self.value,
            "unit": self.unit, "basis": self.basis, "source": self.source,
            "affects": self.affects, "at": self.at,
        }


def _path(conversation_id: str) -> Path:
    return STORE._dir(conversation_id) / _FILE  # noqa: SLF001 — same package's layout


def load(conversation_id: str) -> list[dict[str, Any]]:
    path = _path(conversation_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Unreadable assumption register: %s", path)
        return []
    return raw if isinstance(raw, list) else []


def record(conversation_id: str, **fields: Any) -> dict[str, Any]:
    """Add one assumption. Returns it, so the caller can echo it back.

    Appends rather than replaces: an assumption that was superseded is still part of
    how the model got here, and silently dropping it would make the register a
    snapshot of the agent's current opinion instead of a record.
    """
    basis = str(fields.get("basis") or "assumed")
    entry = Assumption(
        id=uuid.uuid4().hex[:8],
        subject=str(fields.get("subject") or "unnamed"),
        value=str(fields.get("value") if fields.get("value") is not None else ""),
        unit=str(fields.get("unit") or ""),
        basis=basis if basis in BASES else "assumed",
        source=str(fields.get("source") or ""),
        affects=str(fields.get("affects") or ""),
    )
    existing = load(conversation_id)
    existing.append(entry.to_dict())
    path = _path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info(
        "Assumption recorded (%s): %s = %s %s [%s]",
        conversation_id, entry.subject, entry.value, entry.unit, entry.basis,
    )
    return entry.to_dict()
