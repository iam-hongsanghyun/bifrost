"""The HTTP contract Bifrost relies on, pinned with a mock transport.

These run anywhere (no Ragnarok needed). `test_roundtrip_against_ragnarok.py` proves
the same routes against the real engine; this pins what Bifrost SENDS, so a route
rename is a one-line failure here before it is a mystery there.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from bifrost import engine
from bifrost.ragnarok import RagnarokClient


def _client(seen: list[httpx.Request], reply=None) -> RagnarokClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request) if reply else httpx.Response(200, json={})

    return RagnarokClient(
        base="http://engine.test", session_id="s9", transport=httpx.MockTransport(handler)
    )


def test_journal_is_read_from_the_session_router() -> None:
    seen: list[httpx.Request] = []
    asyncio.run(_client(seen).journal(limit=30))
    assert seen[0].url.path == "/api/session/journal"
    assert dict(seen[0].url.params) == {"session_id": "s9", "limit": "30"}


def test_clear_names_the_session_in_the_query_string() -> None:
    seen: list[httpx.Request] = []
    asyncio.run(_client(seen).clear_session())
    assert seen[0].method == "POST" and seen[0].url.path == "/api/session/clear"
    assert seen[0].url.params["session_id"] == "s9"


def test_load_model_posts_the_session_id_and_model() -> None:
    seen: list[httpx.Request] = []
    asyncio.run(_client(seen).load_model({"buses": [{"name": "b"}]}, filename="x.zip"))
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/session/model"
    assert body["sessionId"] == "s9" and body["model"] == {"buses": [{"name": "b"}]}


def test_opening_a_project_with_no_model_clears_instead_of_inheriting() -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/import/project/load":
            return httpx.Response(200, json={"model": {}, "filename": "empty.zip"})
        return httpx.Response(200, json={"cleared": True})

    asyncio.run(_client(seen, reply).open_project(b"zip", "empty.zip"))
    assert [r.url.path for r in seen] == ["/api/import/project/load", "/api/session/clear"]


def test_export_names_the_session_and_expects_bytes() -> None:
    seen: list[httpx.Request] = []
    data = asyncio.run(_client(seen, lambda r: httpx.Response(200, content=b"PK")).export_project())
    assert data == b"PK"
    assert json.loads(seen[0].content)["sessionId"] == "s9"
    assert seen[0].url.path == "/api/export/project"


# ── engine discovery ─────────────────────────────────────────────────────────────


def _checkout(tmp_path: Path, *, mcp: bool = True, venv: bool = True) -> Path:
    root = tmp_path / "ragnarok"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "main.py").write_text("")
    (root / "backend" / "requirements.txt").write_text("")
    if mcp:
        (root / "backend" / "mcp").mkdir()
        (root / "backend" / "mcp" / "__main__.py").write_text("")
    if venv:
        (root / ".venv-pypsa" / "bin").mkdir(parents=True)
        (root / ".venv-pypsa" / "bin" / "python").write_text("")
    return root


def test_a_complete_checkout_is_accepted(tmp_path: Path) -> None:
    check = engine.check_root(str(_checkout(tmp_path)))
    assert check.valid and check.interpreter and check.reason == "Ragnarok found."


def test_a_checkout_without_an_mcp_server_is_valid_but_says_the_agent_has_no_tools(tmp_path: Path) -> None:
    check = engine.check_root(str(_checkout(tmp_path, mcp=False)))
    assert check.valid and "no modelling tools" in check.reason


def test_a_checkout_without_an_environment_says_to_run_ragnaroks_launcher(tmp_path: Path) -> None:
    check = engine.check_root(str(_checkout(tmp_path, venv=False)))
    assert check.valid and check.interpreter is None and "run.command" in check.reason


def test_a_stranger_answering_identity_is_not_an_engine(monkeypatch) -> None:
    import io

    def fake(payload: dict):
        return lambda url, timeout=0: io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake({"root": "/x"}))
    assert engine.served_root("http://h") is None
    monkeypatch.setattr(engine.urllib.request, "urlopen", fake({"app": "ragnarok", "root": "/x"}))
    assert engine.served_root("http://h") == "/x"
