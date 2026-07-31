"""Pending decision-question registry — the bridge between the agent's
``ask_user`` tool and the GUI's option cards.

Same shape as :mod:`.approvals`, different semantics: an approval gates a tool
call the agent already decided on; a question hands the DECISION to the user —
"solve as a single period or a pathway?", "which snapshot window?" — with
concrete options plus free text. The ask_user tool handler awaits the future;
the GUI posts an answer to ``/api/agent/questions/{id}``.

Timeouts resolve with an explicit no-answer sentinel so an unattended agent
proceeds on its own best judgment and says so, instead of hanging.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

QUESTION_TIMEOUT_SECONDS = 600.0
NO_ANSWER = (
    "(no answer from the user — pick the most sensible default, state the "
    "assumption in your reply, and continue)"
)


@dataclass
class Question:
    id: str
    conversation_id: str
    question: str
    options: list[str]
    future: "asyncio.Future[str]" = field(repr=False, default=None)  # type: ignore[assignment]

    def card(self) -> dict[str, Any]:
        return {
            "questionId": self.id,
            "conversationId": self.conversation_id,
            "question": self.question,
            "options": self.options,
        }


_pending: dict[str, Question] = {}


def create(conversation_id: str, question: str, options: list[str]) -> Question:
    q = Question(
        id=uuid.uuid4().hex[:12],
        conversation_id=conversation_id,
        question=question,
        options=[str(o) for o in options if str(o).strip()][:6],
        future=asyncio.get_running_loop().create_future(),
    )
    _pending[q.id] = q
    return q


def resolve(question_id: str, answer: str) -> Question | None:
    """Answer a pending question; returns it, or None if unknown/expired."""
    q = _pending.pop(question_id, None)
    if q is None or q.future.done():
        return None
    q.future.set_result(answer)
    return q


def pending_for(conversation_id: str) -> list[dict[str, Any]]:
    return [q.card() for q in _pending.values() if q.conversation_id == conversation_id]


def cancel_for(conversation_id: str) -> None:
    """Resolve every pending question of a conversation with the no-answer
    sentinel (interrupt / conversation reset)."""
    for question_id in [q.id for q in _pending.values() if q.conversation_id == conversation_id]:
        resolve(question_id, NO_ANSWER)


async def wait(q: Question, timeout: float = QUESTION_TIMEOUT_SECONDS) -> str:
    try:
        return await asyncio.wait_for(q.future, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(q.id, None)
        return NO_ANSWER
