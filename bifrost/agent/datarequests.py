"""Data requests — the agent asking the USER for rows it cannot get from open data.

Same bridge shape as :mod:`.questions`: the ``request_data`` tool awaits a future,
the GUI card resolves it. The difference is WHO acts on the model. The merge is a
mutation the user performs — they typed or dropped those rows into a card the
agent put in front of them — so it needs no second approval and is journalled as
the user's, not the agent's. The agent only learns the outcome: a summary of what
was merged (with the journal ids), or that the user cancelled.

Validation and merging live here, not in the router, so tests drive them without
HTTP; the router (``/api/agent/data-requests``) is a thin shell over them.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import dataask
from ..config import CONFIG
from ..ragnarok import CLIENT, RagnarokError

CANCELLED = (
    "The user CANCELLED this data request: they chose not to supply these rows. "
    "Do NOT invent them. Say what is therefore missing from the model, and either "
    "continue without it or ask what they would prefer."
)
TIMED_OUT = (
    "(no data supplied — the request timed out. Do not invent the rows: say what "
    "is missing and ask again, or continue without it and state that.)"
)


@dataclass
class DataRequest:
    id: str
    conversation_id: str
    spec: dataask.SheetSpec
    reason: str
    example: str = ""
    future: "asyncio.Future[str]" = field(repr=False, default=None)  # type: ignore[assignment]

    def card(self) -> dict[str, Any]:
        needed = set(self.spec.needed)
        return {
            "requestId": self.id,
            "conversationId": self.conversation_id,
            "sheet": self.spec.sheet,
            "component": self.spec.component,
            "reason": self.reason,
            "example": self.example,
            "columns": [c.card(c.name in needed) for c in self.spec.columns],
            "needed": list(self.spec.needed),
        }


_pending: dict[str, DataRequest] = {}


def create(conversation_id: str, spec: dataask.SheetSpec, reason: str, example: str = "") -> DataRequest:
    req = DataRequest(
        id=uuid.uuid4().hex[:12], conversation_id=conversation_id, spec=spec,
        reason=reason, example=example, future=asyncio.get_running_loop().create_future(),
    )
    _pending[req.id] = req
    return req


def get(request_id: str) -> DataRequest | None:
    return _pending.get(request_id)


def pending_for(conversation_id: str) -> list[dict[str, Any]]:
    return [r.card() for r in _pending.values() if r.conversation_id == conversation_id]


def resolve(request_id: str, outcome: str) -> DataRequest | None:
    """Release the waiting tool with ``outcome``; ``None`` if unknown/expired."""
    req = _pending.pop(request_id, None)
    if req is None or req.future.done():
        return None
    req.future.set_result(outcome)
    return req


def cancel_for(conversation_id: str) -> None:
    for request_id in [r.id for r in _pending.values() if r.conversation_id == conversation_id]:
        resolve(request_id, CANCELLED)


async def wait(req: DataRequest, timeout: float | None = None) -> str:
    try:
        return await asyncio.wait_for(req.future, timeout=timeout or CONFIG.dataask_timeout_s)
    except asyncio.TimeoutError:
        _pending.pop(req.id, None)
        return TIMED_OUT


# ── building a request from the live schema ─────────────────────────────────────

async def build_spec(component: str, columns: list[str] | None) -> dataask.SheetSpec:
    """Resolve ``component`` against Ragnarok's live schema (never a stored copy)."""
    config = await CLIENT.config()
    return dataask.spec_from_config(config, component, columns)


async def _names(sheet: str, column: str = "name") -> set[str]:
    try:
        page = await CLIENT.sheet(sheet, 0, CONFIG.table_max_rows)
    except RagnarokError as exc:
        if exc.status == 404:  # no such sheet yet = no rows yet
            return set()
        raise
    return {str(r[column]) for r in (page or {}).get("rows") or [] if r.get(column) not in (None, "")}


async def validate_text(req: DataRequest, text: str) -> dataask.Report:
    """Validate against the model AS IT IS NOW (buses may have changed since the card was raised)."""
    buses = await _names("buses")
    carriers = await _names("carriers")
    existing = await _names(req.spec.sheet)
    return dataask.validate(
        req.spec, text, buses=buses, carriers=carriers, existing_names=existing,
        max_rows=CONFIG.dataask_max_rows,
    )


def _summary(req: DataRequest, report: dataask.Report, merged: dict[str, Any], journal_ids: list[int]) -> dict[str, Any]:
    return {
        "status": "merged",
        "sheet": req.spec.sheet,
        "rowsMerged": len(report.rows),
        "columns": report.columns,
        "renamed": report.renames,
        "carriersAdded": report.new_carriers,
        "journalEntryIds": journal_ids,
        "modelVersion": (merged.get("meta") or {}).get("version"),
        "note": "The user supplied these rows and Ragnarok merged them; they are in the working model now. "
                "Read the sheet back (get_sheet_page) before relying on them, and record_assumption for "
                "any value you still had to fill in yourself.",
    }


async def submit(req: DataRequest, text: str) -> tuple[dataask.Report, dict[str, Any] | None]:
    """Validate, and when clean merge via Ragnarok and release the agent.

    Returns ``(report, summary)``; ``summary`` is ``None`` when validation failed —
    the request then stays pending so the user can fix the rows in the same card.
    """
    report = await validate_text(req, text)
    if not report.ok:
        return report, None
    version_before = None
    try:
        meta = await CLIENT.session_meta()
        version_before = (meta or {}).get("version")
    except Exception:  # noqa: BLE001 — attribution only
        pass
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fragment = dataask.rows_to_fragment(req.spec, report, stamp=stamp)
    merged = await CLIENT.merge_fragment(fragment, conversation_id=req.conversation_id)
    journal_ids: list[int] = []
    if isinstance(version_before, int):
        try:
            entries = ((await CLIENT.journal(limit=30)) or {}).get("entries") or []
            journal_ids = sorted(
                int(e["id"]) for e in entries
                if isinstance(e, dict) and isinstance(e.get("id"), int) and e["id"] > version_before
            )
        except Exception:  # noqa: BLE001
            pass
    summary = _summary(req, report, merged, journal_ids)
    resolve(req.id, json.dumps(summary, ensure_ascii=False))
    return report, summary
