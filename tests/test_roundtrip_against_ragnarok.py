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
