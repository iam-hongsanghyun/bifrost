"""Pending tool-approval registry — the bridge between ``can_use_tool`` and
the GUI's approval cards.

The SDK's permission callback awaits an :class:`Approval`'s future; the GUI
posts a decision to ``/api/agent/approvals/{id}`` which resolves it. Timeouts
resolve as deny so an unattended session re-plans instead of hanging forever.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

APPROVAL_TIMEOUT_SECONDS = 600.0


@dataclass
class Decision:
    allow: bool
    edited_input: dict[str, Any] | None = None
    note: str = ""
    by: str = "user"


@dataclass
class Approval:
    id: str
    conversation_id: str
    tool: str
    args: dict[str, Any]
    future: "asyncio.Future[Decision]" = field(repr=False, default=None)  # type: ignore[assignment]

    def card(self) -> dict[str, Any]:
        return {
            "approvalId": self.id,
            "conversationId": self.conversation_id,
            "tool": self.tool,
            "args": self.args,
        }


_pending: dict[str, Approval] = {}


def create(conversation_id: str, tool: str, args: dict[str, Any]) -> Approval:
    approval = Approval(
        id=uuid.uuid4().hex[:12],
        conversation_id=conversation_id,
        tool=tool,
        args=args,
        future=asyncio.get_running_loop().create_future(),
    )
    _pending[approval.id] = approval
    return approval


def resolve(approval_id: str, decision: Decision) -> Approval | None:
    """Resolve a pending approval; returns it, or None if unknown/expired."""
    approval = _pending.pop(approval_id, None)
    if approval is None or approval.future.done():
        return None
    approval.future.set_result(decision)
    return approval


def pending_for(conversation_id: str) -> list[dict[str, Any]]:
    return [a.card() for a in _pending.values() if a.conversation_id == conversation_id]


def cancel_for(conversation_id: str) -> None:
    """Deny every pending approval of a conversation (user Stop / reset).

    ``by="stop"`` makes the deny ABORT the turn (PermissionResultDeny with
    interrupt=True) instead of feeding back for a re-plan — a Stop must stop.
    """
    for approval_id in [a.id for a in _pending.values() if a.conversation_id == conversation_id]:
        resolve(approval_id, Decision(allow=False, note="The user stopped this turn.", by="stop"))


async def wait(approval: Approval, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> Decision:
    try:
        return await asyncio.wait_for(approval.future, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(approval.id, None)
        return Decision(allow=False, note="approval timed out", by="timeout")
