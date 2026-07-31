"""Questions the agent asks — including the ones that want a FILE.

The gap this covers: Bifrost stored uploads and exposed a `read_attachment` tool
from the start, but nothing in the UI could reach the upload endpoint. So the
agent had a tool for data the user could not hand it, and its only remaining move
was to synthesise a profile — which is the worst failure mode available, because
a fabricated time series looks like measured data once it is in a sheet.

`needsFile` is what lets the agent ASK. The card then offers an upload control,
and the answer comes back as stored paths it can read.
"""
from __future__ import annotations

import asyncio

import pytest

from bifrost.agent import questions


def test_a_plain_question_does_not_ask_for_a_file() -> None:
    async def go() -> None:
        q = questions.create("c1", "Single period or pathway?", ["Single", "Pathway"])
        card = q.card()
        assert card["needsFile"] is False
        assert card["options"] == ["Single", "Pathway"]
        questions.resolve(q.id, "Single")

    asyncio.run(go())


def test_a_file_question_advertises_it_and_keeps_its_way_out() -> None:
    """The options still matter: the user must be able to decline and move on."""
    async def go() -> None:
        q = questions.create(
            "c1", "Do you have a wind profile for this site?",
            ["Synthesise a typical profile instead"],
            needs_file=True,
        )
        card = q.card()
        assert card["needsFile"] is True
        assert card["options"] == ["Synthesise a typical profile instead"]
        questions.resolve(q.id, "- wind.csv → /tmp/uploads/x/wind.csv (12 kB)")

    asyncio.run(go())


def test_the_answer_carries_the_uploaded_paths_back_to_the_agent() -> None:
    """`read_attachment` takes a path, so the path is the payload of the answer."""
    async def go() -> None:
        q = questions.create("c1", "Upload the load trace?", [], needs_file=True)
        answer_text = "I uploaded the file(s) you asked for:\n- load.csv → /u/load.csv (3 kB)"
        waiter = asyncio.ensure_future(questions.wait(q))
        await asyncio.sleep(0)
        assert questions.resolve(q.id, answer_text) is not None
        assert await waiter == answer_text

    asyncio.run(go())


def test_an_unanswered_file_question_does_not_hang_the_conversation() -> None:
    """Timeout resolves with the sentinel, so the agent proceeds and says so
    rather than leaving the user staring at a card forever."""
    async def go() -> None:
        q = questions.create("c1", "Upload a cost table?", [], needs_file=True)
        assert await questions.wait(q, timeout=0.01) == questions.NO_ANSWER

    asyncio.run(go())


def test_cancelling_a_conversation_releases_its_file_questions() -> None:
    async def go() -> None:
        q = questions.create("c9", "Upload a profile?", [], needs_file=True)
        waiter = asyncio.ensure_future(questions.wait(q))
        await asyncio.sleep(0)
        questions.cancel_for("c9")
        assert await waiter == questions.NO_ANSWER
        assert questions.pending_for("c9") == []

    asyncio.run(go())


@pytest.mark.parametrize("basis", ["needsFile", "options", "question"])
def test_every_card_field_the_ui_reads_is_present(basis: str) -> None:
    """The deadlock this project already hit once: the UI dropped a card because a
    field it keyed on was absent, and the agent waited forever."""
    async def go() -> None:
        q = questions.create("c1", "Anything?", ["a"], needs_file=True)
        assert basis in q.card()
        questions.resolve(q.id, "a")

    asyncio.run(go())
