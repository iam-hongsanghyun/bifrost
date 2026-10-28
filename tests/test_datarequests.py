"""The request_data lifecycle against a mock engine: card, validate, merge, cancel, timeout."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bifrost import ragnarok
from bifrost.agent import datarequests, service
from bifrost.main import app

from .test_dataask import CONFIG

state = {"buses": [{"name": "north"}, {"name": "south"}], "carriers": [{"name": "gas"}],
         "gens": [], "version": 4, "merged": []}


def engine(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/config":
        return httpx.Response(200, json=CONFIG)
    if path == "/api/session/sheet/buses":
        return httpx.Response(200, json={"rows": state["buses"]})
    if path == "/api/session/sheet/carriers":
        return httpx.Response(200, json={"rows": state["carriers"]})
    if path == "/api/session/sheet/generators":
        return httpx.Response(404, json={"detail": "no such sheet"})
    if path == "/api/session/meta":
        return httpx.Response(200, json={"version": state["version"]})
    if path == "/api/session/merge-fragment":
        body = json.loads(request.content)
        state["merged"].append((body, request.headers.get("x-ragnarok-conversation"), request.headers.get("x-ragnarok-actor")))
        state["version"] += 1
        return httpx.Response(200, json={"sheets": {"generators": 2}, "applied": True, "meta": {"version": state["version"]}})
    if path == "/api/session/journal":
        return httpx.Response(200, json={"entries": [{"id": state["version"]}, {"id": state["version"] - 1}, {"id": 2}]})
    return httpx.Response(404)


@pytest.fixture()
def wired(monkeypatch):
    state["merged"].clear()
    client = ragnarok.RagnarokClient(base="http://engine.test", transport=httpx.MockTransport(engine))
    for module in (ragnarok, datarequests):
        monkeypatch.setattr(module, "CLIENT", client)
    datarequests._pending.clear()
    yield client
    datarequests._pending.clear()


GOOD = "name,bus,carrier,p_nom\nfleet_a,north,gas,120\nfleet_b,south,gas,80\n"


def ask(args=None):
    session = service.AgentSession("t-data", backend_factory=lambda s: None)  # type: ignore[arg-type]
    session.conversation_id = "conv1"
    return session, asyncio.ensure_future(session._handle_request_data(
        args or {"component": "generators", "columns": ["carrier", "p_nom"], "reason": "private fleet"}))


def test_the_card_comes_from_the_live_schema(wired) -> None:
    async def go():
        session, task = ask()
        await asyncio.sleep(0.05)
        (card,) = datarequests.pending_for("conv1")
        assert card["sheet"] == "generators" and card["needed"] == ["name", "bus", "carrier", "p_nom"]
        assert {c["name"] for c in card["columns"]} >= {"name", "bus", "p_nom", "marginal_cost"}
        assert [c for c in card["columns"] if c["name"] == "p_nom"][0]["unit"] == "MW"
        datarequests.cancel_for("conv1")
        await task
    asyncio.run(go())


def test_a_bad_request_returns_text_to_the_agent_instead_of_raising(wired) -> None:
    async def go():
        _, task = ask({"component": "generators", "columns": ["p_nomm"], "reason": "x"})
        assert "refused" in await task and "valid columns" in await task
        _, task = ask({"component": ""})
        assert "needs a 'component'" in await task
    asyncio.run(go())


def test_submit_with_errors_merges_nothing_and_keeps_the_card_open(wired) -> None:
    async def go():
        _, task = ask()
        await asyncio.sleep(0.05)
        req = next(iter(datarequests._pending.values()))
        report, summary = await datarequests.submit(req, "name,bus,carrier,p_nom\nx,nowhere,gas,1\n")
        assert summary is None and not report.ok
        assert state["merged"] == [] and not task.done()
        report, summary = await datarequests.submit(req, GOOD)
        assert summary and summary["rowsMerged"] == 2
        outcome = json.loads(await task)
        assert outcome["status"] == "merged" and outcome["journalEntryIds"] == [5]  # only entries newer than version 4
    asyncio.run(go())


def test_the_merge_is_the_users_and_needs_no_approval(wired) -> None:
    async def go():
        _, task = ask()
        await asyncio.sleep(0.05)
        await datarequests.submit(next(iter(datarequests._pending.values())), GOOD)
        await task
        body, conversation, actor = state["merged"][-1]
        assert actor is None            # no agent header: the journal tags it `user`
        assert conversation == "conv1"  # but it still links to this chat
        assert body["apply"] is True and body["fragment"]["sheets"]["generators"][0]["name"] == "fleet_a"
    asyncio.run(go())


def test_cancel_tells_the_agent_not_to_invent(wired) -> None:
    async def go():
        _, task = ask()
        await asyncio.sleep(0.05)
        req = next(iter(datarequests._pending.values()))
        datarequests.resolve(req.id, datarequests.CANCELLED)
        assert "Do NOT invent" in await task
        assert datarequests.pending_for("conv1") == []
    asyncio.run(go())


def test_timeout_resolves_without_data(wired) -> None:
    async def go():
        spec = await datarequests.build_spec("generators", [])
        req = datarequests.create("conv1", spec, "r")
        assert "timed out" in await datarequests.wait(req, timeout=0.05)
        assert datarequests.get(req.id) is None
    asyncio.run(go())


def test_routes_validate_submit_and_cancel(wired) -> None:
    http = TestClient(app)

    async def raise_card():
        spec = await datarequests.build_spec("generators", ["p_nom"])
        return datarequests.create("conv1", spec, "r")

    loop = asyncio.new_event_loop()
    req = loop.run_until_complete(raise_card())
    r = http.post(f"/api/agent/data-requests/{req.id}/validate", json={"text": "name,bus,p_nom\nx,nowhere,1\n"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["errors"][0]["line"] == 2
    assert state["merged"] == []
    assert http.post("/api/agent/data-requests/nope/validate", json={"text": "x"}).status_code == 404
    assert http.post(f"/api/agent/data-requests/{req.id}/cancel").json()["resolved"] is True
    assert http.post(f"/api/agent/data-requests/{req.id}/cancel").status_code == 404
    loop.close()


def test_status_lists_pending_data_requests(wired) -> None:
    async def go():
        session, task = ask()
        await asyncio.sleep(0.05)
        assert datarequests.pending_for("conv1")[0]["sheet"] == "generators"
        datarequests.cancel_for("conv1")
        await task
    asyncio.run(go())
