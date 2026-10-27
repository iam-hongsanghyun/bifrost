"""How the agent's MCP subprocess is launched — the seam between Bifrost and the engine.

None of this needs a Claude credential: building the SDK client does not connect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")


@pytest.fixture()
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bifrost import engine
    from bifrost.agent import service
    from bifrost.config import CONFIG

    # A fake engine checkout: only the interpreter path has to exist.
    root = tmp_path / "ragnarok"
    (root / ".venv-pypsa" / "bin").mkdir(parents=True)
    (root / ".venv-pypsa" / "bin" / "python").write_text("")
    monkeypatch.setattr(engine, "load_root", lambda: root)
    monkeypatch.setattr(service, "API_BASE", CONFIG.ragnarok_api_base)
    monkeypatch.setattr(service, "CONFIG", type(CONFIG)(**{**CONFIG.__dict__, "data_dir": tmp_path / "data"}))

    session = service.AgentSession("t-session")
    session.workdir = tmp_path / "data" / "agent" / "workdir"
    client = service._sdk_backend(session)
    return root, session, client.options


def test_the_engines_own_interpreter_runs_its_own_mcp_server(launch) -> None:
    root, _, options = launch
    server = options.mcp_servers["ragnarok"]
    assert server["command"] == str(root / ".venv-pypsa" / "bin" / "python")
    assert server["args"] == ["-m", "backend.mcp"]
    assert server["env"]["PYTHONPATH"] == str(root)


def test_ragnaroks_confirm_gate_is_off_so_bifrosts_is_the_only_one(launch) -> None:
    """If Ragnarok's gate stayed on, an approved call would come back as a "preview"
    and the agent would have to re-send it with confirm=true: two gates, stacked."""
    _, _, options = launch
    env = options.mcp_servers["ragnarok"]["env"]
    assert env["RAGNAROK_MCP_AUTONOMY"] == "auto"
    assert env["RAGNAROK_MCP_ACTOR"] == "agent"


def test_the_mcp_server_talks_to_the_engine_bifrost_talks_to(launch) -> None:
    from bifrost.config import CONFIG

    _, session, options = launch
    env = options.mcp_servers["ragnarok"]["env"]
    assert env["RAGNAROK_API_BASE"] == CONFIG.ragnarok_api_base
    assert env["RAGNAROK_SESSION_ID"] == session.session_id
    assert env["RAGNAROK_MCP_CONVERSATION"] == session.conversation_id


def test_the_mcp_server_does_not_start_or_stop_an_engine_of_its_own(launch) -> None:
    _, _, options = launch
    env = options.mcp_servers["ragnarok"]["env"]
    assert env["RAGNAROK_MCP_AUTOSTART"] == "0"
    assert env["RAGNAROK_MCP_TRANSPORT"] == "stdio"


def test_exports_land_in_the_agents_workdir(launch) -> None:
    _, session, options = launch
    assert options.mcp_servers["ragnarok"]["env"]["RAGNAROK_MCP_FILE_DIR"] == str(session.workdir)
    assert options.cwd == str(session.workdir)


def test_no_engine_means_no_ragnarok_server_not_a_broken_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from bifrost import engine
    from bifrost.agent import service

    monkeypatch.setattr(engine, "load_root", lambda: None)
    options = service._sdk_backend(service.AgentSession("t")).options
    assert "ragnarok" not in options.mcp_servers and "bifrost" in options.mcp_servers


def test_only_engine_and_bifrost_tools_pass_the_permission_callback() -> None:
    import asyncio

    from bifrost.agent import service

    session = service.AgentSession("t")

    async def go():
        deny = await session._can_use_tool("Bash", {"command": "ls"}, None)
        allow = await session._can_use_tool("mcp__ragnarok__get_kpis", {"run_name": "r"}, None)
        return deny, allow

    deny, allow = asyncio.run(go())
    assert deny.behavior == "deny" and allow.behavior == "allow"
