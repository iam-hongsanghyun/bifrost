"""request_data against the REAL Ragnarok: its live schema, its merge, its journal.

Drives Ragnarok's actual app in-process (ASGI transport), as the roundtrip test
does. What this proves that the mock cannot: the schema bundle Bifrost validates
against is the one the engine serves; the fragment Bifrost builds is accepted by
the engine's merge; the rows land in the session; and the journal records the
merge as the USER's, linked to the conversation.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from .test_roundtrip_against_ragnarok import _load_ragnarok_app

MODEL = {
    "buses": [{"name": "north", "v_nom": 380.0}, {"name": "south", "v_nom": 380.0}],
    "carriers": [{"name": "gas"}],
    "generators": [{"name": "existing", "bus": "north", "carrier": "gas", "p_nom": 50.0}],
}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    app = _load_ragnarok_app()
    from backend.app import session_store as ragnarok_sessions  # type: ignore

    monkeypatch.setattr(ragnarok_sessions, "SESSION_DIR", tmp_path / "ragnarok-session")
    monkeypatch.setenv("RAGNAROK_JOURNAL_DIR", str(tmp_path / "ragnarok-journal"))
    from bifrost import ragnarok
    from bifrost.agent import datarequests

    client = ragnarok.RagnarokClient(
        base="http://ragnarok.test", session_id="dataask", transport=httpx.ASGITransport(app=app))
    monkeypatch.setattr(ragnarok, "CLIENT", client)
    monkeypatch.setattr(datarequests, "CLIENT", client)
    datarequests._pending.clear()
    from fastapi.testclient import TestClient

    http = TestClient(app)
    r = http.post("/api/session/model", json={"sessionId": "dataask", "model": MODEL, "filename": "m.xlsx"})
    assert r.status_code == 200, r.text
    return client, http, datarequests


def test_private_fleet_roundtrip_validates_merges_and_is_journalled_as_the_user(wired) -> None:
    client, http, datarequests = wired

    async def scenario() -> None:
        spec = await datarequests.build_spec("generators", ["carrier", "p_nom", "marginal_cost"])
        assert spec.sheet == "generators" and spec.column("p_nom").unit == "MW"
        req = datarequests.create("conv-live", spec, "private fleet")

        # One bad row: a bus that is not in the model, and a unit the schema cannot convert.
        bad = ("name,bus,carrier,p_nom [kW],marginal_cost\n"
               "plant_a,north,gas,80000,31\n"
               "plant_b,nowhere,gas,60000,33\n")
        report, summary = await datarequests.submit(req, bad)
        assert summary is None and [e["line"] for e in report.errors] == [3]
        assert len((await client.sheet("generators"))["rows"]) == 1     # nothing merged

        good = ("name,bus,carrier,p_nom [kW],marginal_cost\n"
                "plant_a,north,gas,80000,31\n"
                "plant_b,south,biomass,60000,33\n"
                "existing,south,gas,1000,40\n")
        before = (await client.session_meta())["version"]
        report, summary = await datarequests.submit(req, good)
        assert summary and summary["rowsMerged"] == 3
        assert summary["carriersAdded"] == ["biomass"]
        assert summary["renamed"] == {"existing": "existing_2"}

        rows = {r["name"]: r for r in (await client.sheet("generators"))["rows"]}
        assert set(rows) == {"existing", "plant_a", "plant_b", "existing_2"}
        assert rows["plant_a"]["p_nom"] == pytest.approx(80.0)          # kW -> MW
        assert rows["plant_b"]["bus"] == "south" and rows["plant_b"]["marginal_cost"] == 33
        assert "biomass" in {r["name"] for r in (await client.sheet("carriers"))["rows"]}
        assert json.loads(await req.future)["status"] == "merged"       # the agent is released

        ids = summary["journalEntryIds"]
        assert ids and all(i > before for i in ids)
        entries = {e["id"]: e for e in (await client.journal(limit=30))["entries"]}
        entry = entries[ids[0]]
        assert entry.get("actor") == "user", entry          # not "agent": the person supplied the rows
        assert (entry.get("origin") or {}).get("conversationId") == "conv-live"

    asyncio.run(scenario())


def test_the_schema_the_card_shows_is_the_engines_not_a_copy(wired) -> None:
    client, _, datarequests = wired

    async def scenario() -> None:
        spec = await datarequests.build_spec("Line", [])
        assert spec.sheet == "lines" and {"bus0", "bus1"} <= set(spec.needed)
        report = (await datarequests.validate_text(
            datarequests.create("c", spec, "r"), "name,bus0,bus1,x,r\nL1,north,south,0.1,0.01\nL2,north,mars,0.1,0.01\n"))
        assert [(e["line"], e["column"]) for e in report.errors] == [(3, "bus1")]

    asyncio.run(scenario())


def test_reopening_the_live_conversation_keeps_newer_work_and_adds_no_journal_noise(wired, tmp_path, monkeypatch) -> None:
    """Found in the live run: a page reload re-activated the active conversation, which
    loaded its (older) stored package over the live session and journalled
    'user: replace model' each time. The live session is the truth for the active chat."""
    client, http, _ = wired
    from fastapi.testclient import TestClient

    from bifrost import ragnarok
    from bifrost.conversations import router as conv_router
    from bifrost.conversations.store import ConversationStore

    store = ConversationStore(root=tmp_path / "conversations")
    monkeypatch.setattr(conv_router, "STORE", store)
    monkeypatch.setattr(conv_router, "CLIENT", client)
    conversation = store.create(title="live", group="g")

    async def snapshot() -> int:
        package = await client.export_project()
        version = (await client.session_meta())["version"]
        store.save_project(conversation.id, package, version=version)
        store.set_active(conversation.id)
        return version

    asyncio.run(snapshot())
    # Work happens after the snapshot: the stored package is now OLDER than the session.
    http.post("/api/session/model", json={"sessionId": "dataask", "model": {**MODEL, "buses": MODEL["buses"] + [{"name": "east"}]}})
    version_before = asyncio.run(client.session_meta())["version"]

    from bifrost.main import app as bifrost_app

    reply = TestClient(bifrost_app).post(f"/api/conversations/{conversation.id}/activate")
    assert reply.status_code == 200, reply.text
    assert reply.json()["loaded"] == {"adopted": True}
    assert asyncio.run(client.session_meta())["version"] == version_before     # nothing journalled
    names = {r["name"] for r in asyncio.run(client.sheet("buses"))["rows"]}
    assert names == {"north", "south", "east"}                                  # newer work survived
