"""``/api/state/*`` against a mock Ragnarok: routes, parameters sent, errors surfaced."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bifrost import state_router
from bifrost.main import app
from bifrost.ragnarok import RagnarokClient

from .test_state import GENS, META, MODEL, _analytics

TS = ["2030-06-01T00:00:00", "2030-06-01T01:00:00", "2030-06-01T02:00:00"]
seen: list[httpx.Request] = []


def engine(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    path = request.url.path
    if path == "/api/session/meta":
        return httpx.Response(200, json={**META, "version": 7})
    if path == "/api/session/model/full":
        return httpx.Response(200, json={"model": MODEL})
    if path.startswith("/api/session/sheet/"):
        return httpx.Response(200, json={"name": "generators", "kind": "static", "columns": ["name", "p_nom"],
                                         "rows": [{"name": "a", "p_nom": 2}, {"name": "b", "p_nom": 9}]})
    if path == "/api/session/journal":
        return httpx.Response(200, json={"version": 3, "entries": [{"id": 3}, {"id": 2}]})
    if path == "/api/session/journal/3/diff":
        return httpx.Response(200, json={"id": 3, "detail": {"cellDiffs": []}})
    if path == "/api/queue" and request.method == "GET":
        return httpx.Response(200, json={"jobs": [{"id": "j", "status": "queued"}], "concurrency": 1, "maxConcurrency": 8})
    if path == "/api/queue" and request.method == "POST":
        body = json.loads(request.content)
        if body["scenario"].get("boom"):
            return httpx.Response(400, json={"detail": "discountRate is required for THIS run"})
        return httpx.Response(200, json={"id": "j2", "status": "queued", "position": 1})
    if path == "/api/queue/j/cancel":
        return httpx.Response(200, json={"cancelled": True})
    if path == "/api/runs":
        return httpx.Response(200, json={"runs": [{"name": "r1", "label": "Run", "summary": [{"label": "k", "value": "v"}]}]})
    if path.endswith("/analytics"):
        return httpx.Response(200, json=_analytics())
    if path.endswith("/kpis"):
        return httpx.Response(200, json={"run": "r", "kpis": {"avgPrice": 5.0, "currencySymbol": "$"}})
    if path.endswith("/model/sheet/generators"):
        return httpx.Response(200, json={"rows": GENS})
    if path.endswith("/model/sheet/storage_units"):
        return httpx.Response(404, json={"detail": "nope"})
    if path.endswith("/series/generators-p"):
        return httpx.Response(200, json={"indexCol": "snapshot", "columns": ["snapshot", "wind", "gas"],
                                         "rows": [{"snapshot": t, "wind": 1.0, "gas": 2.0} for t in TS]})
    if "/series/" in path:
        return httpx.Response(404, json={"detail": "Run series not found."})
    return httpx.Response(404, json={"detail": f"unexpected {path}"})


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    seen.clear()
    monkeypatch.setattr(state_router, "CLIENT", RagnarokClient(
        base="http://engine.test", session_id="s1", transport=httpx.MockTransport(engine)))
    return TestClient(app)


def test_model_route_combines_meta_and_static_sheets(client: TestClient) -> None:
    body = client.get("/api/state/model").json()
    assert body["loaded"] and body["version"] == 7
    assert {c["sheet"] for c in body["counts"]} >= {"buses", "generators", "lines"}
    assert {r.url.path for r in seen} == {"/api/session/meta", "/api/session/model/full"}
    assert all(r.url.params["session_id"] == "s1" for r in seen)


def test_sheet_route_sorts_filters_and_wraps_in_items_total(client: TestClient) -> None:
    body = client.get("/api/state/sheet/generators", params={"sort": "p_nom", "desc": "true", "limit": 1}).json()
    assert body["total"] == 2 and [r["name"] for r in body["items"]] == ["b"]


def test_sheet_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/state/sheet/generators", params={"limit": 100000}).status_code == 422


def test_journal_route_pages_and_diff_is_passed_through(client: TestClient) -> None:
    body = client.get("/api/state/journal", params={"limit": 2, "before": 10}).json()
    assert body["total"] == 2 and body["next_before"] == 2 and body["version"] == 3
    sent = [r for r in seen if r.url.path == "/api/session/journal"][0]
    assert sent.url.params["before"] == "10" and sent.url.params["limit"] == "2"
    assert client.get("/api/state/journal/3/diff").json()["id"] == 3


def test_queue_and_runs_are_wrapped(client: TestClient) -> None:
    q = client.get("/api/state/queue").json()
    assert q["items"][0]["id"] == "j" and q["active"] == 1
    assert client.get("/api/state/runs").json()["items"][0]["name"] == "r1"


def test_solve_posts_the_session_and_cancel_hits_the_job(client: TestClient) -> None:
    out = client.post("/api/state/queue", json={"scenario": {"discountRate": 0.05}}).json()
    assert out["id"] == "j2"
    posted = [r for r in seen if r.method == "POST"][0]
    assert json.loads(posted.content) == {"sessionId": "s1", "scenario": {"discountRate": 0.05}, "options": {}}
    assert client.post("/api/state/queue/j/cancel").json() == {"cancelled": True}


def test_ragnarok_error_text_reaches_the_user_unwrapped(client: TestClient) -> None:
    r = client.post("/api/state/queue", json={"scenario": {"boom": True}})
    assert r.status_code == 400 and r.json()["detail"] == "discountRate is required for THIS run"


def test_unreachable_engine_is_503_with_a_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(state_router, "CLIENT", RagnarokClient(
        base="http://engine.test", session_id="s1", transport=httpx.MockTransport(refuse)))
    r = TestClient(app).get("/api/state/queue")
    assert r.status_code == 503 and "Cannot reach Ragnarok" in r.json()["detail"]


def test_dashboard_route(client: TestClient) -> None:
    body = client.get("/api/state/runs/r/dashboard").json()
    assert body["x"][0] == "2030-06-01 00:00" and body["dispatch_mw"]["series"]
    assert not any("/series/" in r.url.path for r in seen), "no per-asset read unless a bus is chosen"
    assert body["storage"]["soc_mwh"] == []  # a run with no storage sheet (404) is normal


def test_dashboard_with_a_bus_reads_the_whole_per_asset_series(client: TestClient) -> None:
    body = client.get("/api/state/runs/r/dashboard", params={"bus": "n"}).json()
    assert [s["name"] for s in body["dispatch_mw"]["series"]] == ["wind"]
    series = [r for r in seen if r.url.path.endswith("/series/generators-p")][0]
    assert series.url.params["maxPoints"] == "3", "must not take Ragnarok's 800-point default"


def test_snapshot_route_reads_one_row_of_each_sheet(client: TestClient) -> None:
    body = client.get("/api/state/runs/r/snapshot", params={"index": 2}).json()
    assert body["index"] == 2 and {c["name"] for c in body["by_carrier"]} == {"wind", "gas"}
    gen = [r for r in seen if r.url.path.endswith("/series/generators-p")][0]
    assert (gen.url.params["start"], gen.url.params["end"]) == ("2", "3")
    assert client.get("/api/state/runs/r/snapshot", params={"index": -1}).status_code == 422


def test_compare_needs_two_to_five_runs(client: TestClient) -> None:
    assert client.get("/api/state/compare", params={"runs": "a"}).status_code == 400
    assert client.get("/api/state/compare", params={"runs": "a,b,c,d,e,f"}).status_code == 400
    body = client.get("/api/state/compare", params={"runs": "a,b"}).json()
    assert [r["name"] for r in body["runs"]] == ["a", "b"] and body["kpis"]
