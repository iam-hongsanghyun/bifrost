"""The core claim, end to end against the real Ragnarok app.

    Reopening a conversation restores the model it was produced against.

Everything else in Bifrost is arrangement; if this is false the product is lying to
the user, because a transcript saying "I set the carbon price to 80" would be
describing a model that is no longer loaded.

Ragnarok is driven **in-process** over an ASGI transport, so this is a real
integration test — its actual export/import endpoints, its actual session store —
with no server to start. Skipped rather than failed when Ragnarok is not importable
(it is a sibling checkout, not a pip dependency), so Bifrost's own suite stays
runnable on its own.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

RAGNAROK_ROOT = Path(
    os.environ.get("RAGNAROK_ROOT", Path.home() / "github" / "ragnarok")
)

pytest.importorskip("httpx")


def _load_ragnarok_app():
    """Import Ragnarok's FastAPI app from the sibling checkout, or skip."""
    if not (RAGNAROK_ROOT / "backend" / "app" / "main.py").exists():
        pytest.skip(f"Ragnarok checkout not found at {RAGNAROK_ROOT}")
    if str(RAGNAROK_ROOT) not in sys.path:
        sys.path.insert(0, str(RAGNAROK_ROOT))
    try:
        from backend.app.main import app  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — pypsa etc. may be absent
        pytest.skip(f"Ragnarok not importable ({exc.__class__.__name__}: {exc})")
    return app


SNAPS = [f"2030-01-01T{h:02d}:00:00" for h in range(4)]
MODEL = {
    "buses": [{"name": "b1", "v_nom": 380.0}],
    "snapshots": [{"snapshot": s} for s in SNAPS],
    "loads": [{"name": "L", "bus": "b1", "p_set": 100.0}],
    "loads-p_set": [{"snapshot": s, "L": 10.0 * i} for i, s in enumerate(SNAPS)],
    "generators": [
        {"name": "g", "bus": "b1", "carrier": "gas", "p_nom": 500.0, "marginal_cost": 10.0}
    ],
    "carriers": [{"name": "gas"}],
}


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Bifrost store + a client wired to Ragnarok's app in-process."""
    import httpx

    app = _load_ragnarok_app()

    # Ragnarok must write its session + journal under tmp, not into its checkout.
    from backend.app import session_store as ragnarok_sessions  # type: ignore

    monkeypatch.setattr(ragnarok_sessions, "SESSION_DIR", tmp_path / "ragnarok-session")
    monkeypatch.setenv("RAGNAROK_JOURNAL_DIR", str(tmp_path / "ragnarok-journal"))

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bifrost.conversations.store import ConversationStore  # Bifrost's
    from bifrost.ragnarok import RagnarokClient

    client = RagnarokClient(
        base="http://ragnarok.test",
        session_id="default",
        transport=httpx.ASGITransport(app=app),
    )
    return ConversationStore(root=tmp_path / "conversations"), client, app


def test_a_conversations_model_survives_being_put_away_and_reopened(wired) -> None:
    store, client, app = wired
    from fastapi.testclient import TestClient

    http = TestClient(app)

    async def scenario() -> None:
        # 1. The user (or the agent) builds a model in the shared session.
        resp = http.post(
            "/api/session/model",
            json={"sessionId": "default", "model": MODEL, "filename": "case.xlsx"},
        )
        assert resp.status_code == 200, resp.text

        # 2. The conversation captures it — this is `POST /snapshot`.
        conversation = store.create(title="Peak week", group="Korea 2030")
        meta_before = await client.session_meta()
        package = await client.export_project()
        store.save_project(conversation.id, package, version=meta_before.get("version"))
        assert store.has_project(conversation.id)

        # 3. The user moves on: a different conversation clears the session.
        await client.clear_session()
        assert await client.session_meta() == {}

        # 4. Reopening the conversation — this is `POST /activate`.
        bundle = await client.import_project(
            store.read_project(conversation.id), f"{conversation.id}.zip"
        )

        # The model is back, INCLUDING its time-series. The series are the part a
        # browser never holds, so they are the part a naive implementation loses.
        model = bundle.get("model") or {}
        assert model.get("buses"), "topology lost"
        assert len(model.get("loads-p_set") or []) == len(SNAPS), "time-series lost"
        assert [row["L"] for row in model["loads-p_set"]] == [0.0, 10.0, 20.0, 30.0]
        assert len(model.get("snapshots") or []) == len(SNAPS), "time axis lost"

    asyncio.run(scenario())


def test_activating_a_conversation_with_no_project_clears_rather_than_inherits(wired) -> None:
    """A fresh conversation must not silently adopt the previous one's model.

    If it did, the agent would reason about a model the user never mentioned in
    this chat — the worst kind of wrong, because everything it says would be
    internally consistent.
    """
    store, client, app = wired
    from fastapi.testclient import TestClient

    http = TestClient(app)

    async def scenario() -> None:
        http.post(
            "/api/session/model",
            json={"sessionId": "default", "model": MODEL, "filename": "case.xlsx"},
        )
        assert await client.session_meta() != {}

        fresh = store.create(title="Unrelated question")
        assert store.read_project(fresh.id) is None
        await client.clear_session()  # what /activate does when there is no package

        assert await client.session_meta() == {}

    asyncio.run(scenario())


# ── routes Bifrost calls, checked against the current engine ─────────────────────


def _put(http, session: str, model=MODEL) -> None:
    resp = http.post(
        "/api/session/model", json={"sessionId": session, "model": model, "filename": "case.xlsx"}
    )
    assert resp.status_code == 200, resp.text


def test_opening_a_project_loads_it_into_the_session(wired) -> None:
    """`/api/import/project/load` only PARSES a package; it stores nothing.

    Bifrost's activate used to stop there, so "restoring" a conversation returned a
    bundle while the session stayed empty (or kept the previous model).
    """
    store, client, app = wired
    from fastapi.testclient import TestClient

    http = TestClient(app)

    async def scenario() -> None:
        _put(http, "default")
        package = await client.export_project()
        await client.clear_session()

        # The engine's own import route leaves the session untouched...
        await client.import_project(package, "p.zip")
        assert await client.session_meta() == {}
        # ...which is why Bifrost loads the bundle's model itself.
        bundle = await client.open_project(package, "p.zip")
        meta = await client.session_meta()
        assert meta.get("componentCounts", {}).get("buses") == 1
        assert meta["snapshotCount"] == len(SNAPS)
        assert bundle["model"]["loads-p_set"], "series must come back too"

    asyncio.run(scenario())


def test_clearing_one_session_leaves_the_others_alone(wired) -> None:
    """The session id is a QUERY parameter on `/api/session/clear`. Sent as a body
    it was ignored and the `default` session was cleared whatever id was meant."""
    store, client, app = wired
    import httpx
    from fastapi.testclient import TestClient

    from bifrost.ragnarok import RagnarokClient

    http = TestClient(app)
    other = RagnarokClient(
        base="http://ragnarok.test", session_id="bifrost-other",
        transport=httpx.ASGITransport(app=app),
    )

    async def scenario() -> None:
        _put(http, "default")
        _put(http, "bifrost-other")
        await other.clear_session()
        assert await other.session_meta() == {}
        assert await client.session_meta() != {}, "cleared the wrong session"

    asyncio.run(scenario())


def test_the_journal_route_exists_and_reports_the_version(wired) -> None:
    store, client, app = wired
    from fastapi.testclient import TestClient

    http = TestClient(app)

    async def scenario() -> None:
        _put(http, "default")
        payload = await client.journal(limit=5)
        assert isinstance(payload.get("entries"), list)
        assert isinstance(payload.get("version"), int)
        assert (await client.session_meta())["version"] == payload["version"]

    asyncio.run(scenario())


def test_engine_identity_and_status_answer(wired) -> None:
    store, client, app = wired

    async def scenario() -> None:
        who = await client.identity()
        assert who["app"] == "ragnarok" and who["root"]
        assert "ready" in await client.status()

    asyncio.run(scenario())


def test_activating_a_conversation_over_http_restores_the_session(wired, monkeypatch) -> None:
    """The whole switch, through Bifrost's own router: snapshot → move on → activate."""
    store, client, app = wired
    from fastapi.testclient import TestClient

    from bifrost.conversations import router as conv_router
    from bifrost.main import app as bifrost_app

    monkeypatch.setattr(conv_router, "STORE", store)
    monkeypatch.setattr(conv_router, "CLIENT", client)
    monkeypatch.setattr("bifrost.assumptions.load", lambda _id: [])
    engine_http = TestClient(app)
    ui = TestClient(bifrost_app)

    first = ui.post("/api/conversations", json={"title": "Peak week"}).json()
    second = ui.post("/api/conversations", json={"title": "Other"}).json()
    assert ui.post(f"/api/conversations/{first['id']}/activate").status_code == 200

    _put(engine_http, "default")
    assert ui.post(f"/api/conversations/{first['id']}/snapshot").json()["hasProject"]

    # Switching away captures the model, then the new (empty) conversation clears.
    assert ui.post(f"/api/conversations/{second['id']}/activate").json()["loaded"] is None
    assert engine_http.get("/api/session/meta").json() == {}

    back = ui.post(f"/api/conversations/{first['id']}/activate").json()
    assert back["loaded"]["hasResult"] is False and "buses" in back["loaded"]["sheets"]
    meta = engine_http.get("/api/session/meta").json()
    assert meta["snapshotCount"] == len(SNAPS) and meta["componentCounts"]["buses"] == 1


def test_importing_a_package_makes_it_a_live_conversation(wired, monkeypatch) -> None:
    store, client, app = wired
    from fastapi.testclient import TestClient

    from bifrost.conversations import router as conv_router
    from bifrost.main import app as bifrost_app

    monkeypatch.setattr(conv_router, "STORE", store)
    monkeypatch.setattr(conv_router, "CLIENT", client)
    monkeypatch.setattr("bifrost.assumptions.load", lambda _id: [])
    engine_http = TestClient(app)
    ui = TestClient(bifrost_app)

    _put(engine_http, "default")
    package = asyncio.run(client.export_project())
    engine_http.post("/api/session/clear", params={"session_id": "default"})

    resp = ui.post("/api/conversations/import", files={"file": ("study.zip", package, "application/zip")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "study" and body["hasProject"]
    assert engine_http.get("/api/session/meta").json()["componentCounts"]["buses"] == 1
    assert store.get_active() == body["id"]


# ── the two approval gates must not stack ────────────────────────────────────────


def _call_mcp_tool(app, autonomy: str, monkeypatch: pytest.MonkeyPatch, tool: str, args: dict):
    """Call a real Ragnarok MCP tool, wired to the real app over ASGI."""
    import httpx

    try:
        from backend.mcp import server as mcp_server  # type: ignore[import-not-found]
        from backend.mcp.client import RagnarokClient as McpClient  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — the mcp extra may be absent
        pytest.skip(f"Ragnarok's MCP server is not importable ({exc.__class__.__name__}: {exc})")

    monkeypatch.setenv("RAGNAROK_MCP_AUTONOMY", autonomy)
    monkeypatch.setenv("RAGNAROK_MCP_ACTOR", "agent")
    monkeypatch.setenv("RAGNAROK_SESSION_ID", "default")  # what service.py passes per session
    monkeypatch.setattr(
        mcp_server, "_client", McpClient(transport=httpx.ASGITransport(app=app)), raising=False
    )

    async def go():
        result = await mcp_server.mcp.call_tool(tool, args)
        return result

    return asyncio.run(go())


def test_under_the_mode_bifrost_launches_the_engines_gate_does_not_fire(wired, monkeypatch) -> None:
    """`clear_session` is a GATE tool. Under the engine's default it returns a preview
    and changes nothing — the "double block" if Bifrost's approval card had already
    been accepted. Under `auto` (what Bifrost sets) it just runs."""
    store, client, app = wired
    from fastapi.testclient import TestClient

    http = TestClient(app)

    _put(http, "default")
    _call_mcp_tool(app, "guided", monkeypatch, "clear_session", {})
    assert await_meta(client) != {}, "guided should have returned a preview, not cleared"

    _call_mcp_tool(app, "auto", monkeypatch, "clear_session", {})
    assert await_meta(client) == {}, "auto must run the tool once the user has approved it"


def await_meta(client):
    return asyncio.run(client.session_meta())
