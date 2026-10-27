"""The conversation router's chat handling: what it sends Ragnarok on snapshot and
what it takes back on import, with a stand-in engine (no Ragnarok needed)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost.conversations import router as conv_router
from bifrost.conversations.store import ConversationStore
from bifrost.main import app

POINTS = 2000


def _chart() -> dict[str, Any]:
    return {
        "kind": "chart.show", "chartId": "ch1", "title": "Dispatch", "chartKind": "line",
        "x": [f"t{i}" for i in range(POINTS)],
        "series": [{"name": "gas", "values": [1.0] * POINTS}],
    }


class FakeEngine:
    """Records the conversation sent on export; returns a canned bundle on import."""

    def __init__(self, bundle: dict[str, Any] | None = None) -> None:
        self.exports: list[dict[str, Any] | None] = []
        self.bundle = bundle if bundle is not None else {"model": {}, "filename": "p"}
        self.session_id = "default"

    async def session_meta(self) -> dict[str, Any]:
        return {"version": 4}

    async def export_project(self, *, conversation: dict[str, Any] | None = None, **_: Any) -> bytes:
        self.exports.append(conversation)
        return b"PK\x03\x04pkg"

    async def import_project(self, data: bytes, filename: str) -> dict[str, Any]:
        return self.bundle

    async def open_project(self, data: bytes, filename: str) -> dict[str, Any]:
        return self.bundle

    async def clear_session(self) -> None:
        return None


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = ConversationStore(root=tmp_path / "conversations")

    def make(engine: FakeEngine) -> TestClient:
        monkeypatch.setattr(conv_router, "STORE", store)
        monkeypatch.setattr(conv_router, "CLIENT", engine)
        monkeypatch.setattr("bifrost.assumptions.load", lambda _id: [])
        return TestClient(app)

    return store, make


def test_snapshot_sends_the_transcript_with_its_charts_inline(wired) -> None:
    store, make = wired
    engine = FakeEngine()
    ui = make(engine)
    c = store.create(title="Peak week")
    store.save_events(c.id, [{"kind": "user", "text": "plot it"}, _chart()])

    assert ui.post(f"/api/conversations/{c.id}/snapshot").status_code == 200

    sent = engine.exports[0]
    assert sent is not None
    assert sent["conversationId"] == c.id and sent["title"] == "Peak week"
    assert {"model", "autonomy", "version"} <= set(sent)
    chart = sent["events"][1]
    assert len(chart["x"]) == POINTS and "lazy" not in chart, "the package must be self-contained"


def test_snapshot_of_a_conversation_with_no_transcript_sends_no_sidecar(wired) -> None:
    store, make = wired
    engine = FakeEngine()
    ui = make(engine)
    c = store.create()
    assert ui.post(f"/api/conversations/{c.id}/snapshot").status_code == 200
    assert engine.exports == [None]


def test_the_conversation_endpoint_is_light_and_the_chart_endpoint_has_the_points(wired) -> None:
    store, make = wired
    ui = make(FakeEngine())
    c = store.create()
    store.save_events(c.id, [_chart()])

    body = ui.get(f"/api/conversations/{c.id}").json()
    assert body["events"][0]["lazy"] is True and "x" not in body["events"][0]
    assert len(json.dumps(body)) < 2000

    points = ui.get(f"/api/conversations/{c.id}/charts/ch1")
    assert points.status_code == 200 and len(points.json()["x"]) == POINTS
    assert ui.get(f"/api/conversations/{c.id}/charts/missing").status_code == 404
    assert ui.get("/api/conversations/nope/charts/ch1").status_code in (404, 400, 500)


def _zip_with_sidecar(events: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("p.conversation.json", json.dumps({"events": events}))
    return buf.getvalue()


def _import(ui: TestClient, data: bytes) -> dict[str, Any]:
    resp = ui.post("/api/conversations/import", files={"file": ("study.zip", data, "application/zip")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_import_prefers_the_conversation_ragnarok_returns(wired) -> None:
    store, make = wired
    from_bundle = [{"kind": "user", "text": "from the bundle"}]
    ui = make(FakeEngine({"model": {}, "filename": "p", "conversation": {"events": from_bundle}}))
    body = _import(ui, _zip_with_sidecar([{"kind": "user", "text": "from the zip"}]))
    assert [e["text"] for e in store.events(body["id"])] == ["from the bundle"]


def test_import_trusts_a_null_conversation_from_a_current_ragnarok(wired) -> None:
    store, make = wired
    ui = make(FakeEngine({"model": {}, "filename": "p", "conversation": None}))
    body = _import(ui, _zip_with_sidecar([{"kind": "user", "text": "ignored"}]))
    assert store.events(body["id"]) == []


def test_import_falls_back_to_the_zip_for_an_older_ragnarok(wired) -> None:
    store, make = wired
    ui = make(FakeEngine({"model": {}, "filename": "p"}))  # no `conversation` key at all
    body = _import(ui, _zip_with_sidecar([{"kind": "user", "text": "from the zip"}, _chart()]))
    events = store.events(body["id"])
    assert events[0]["text"] == "from the zip"
    assert events[1]["lazy"] is True and len(store.chart(body["id"], "ch1")["x"]) == POINTS
