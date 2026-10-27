"""The approval policy against Ragnarok's real tool catalogue.

The policy is a list of names; the engine decides which names exist. They drifted
once — the server grew from 73 tools to 154 while the policy stayed put, and every
tool it had not heard of flowed unasked under ``auto``. These tests make that
unrepresentable: a new engine tool that is not classified fails here, and an
unclassified tool asks for approval at runtime regardless.

The offline half reads a committed snapshot (``fixtures/ragnarok_tools.json``); the
live half asks the engine checkout's own interpreter, and skips when there is none.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bifrost.agent import catalog, policy

SNAPSHOT = json.loads(
    (Path(__file__).parent / "fixtures" / "ragnarok_tools.json").read_text("utf-8")
)["tools"]
NAMES = {t["name"] for t in SNAPSHOT}
RAGNAROK_ROOT = Path(os.environ.get("RAGNAROK_ROOT", Path.home() / "github" / "ragnarok"))

#: Engine-annotated as writing, yet never touching the working model — read-only here.
READ_ONLY_EXCEPTIONS = {"cancel_solve", "physical_risk_transition", "physical_risk_finance"}


def _gated(name: str, autonomy: str, tool_input: dict | None = None, workdir: str | None = None) -> bool:
    return policy.requires_approval(policy.MCP_PREFIX + name, autonomy, tool_input, workdir)


def test_the_tiers_are_disjoint() -> None:
    tiers = [policy.READ_ONLY, policy.EXPORTS, policy.CHEAP_EDITS, policy.ALWAYS_GATED, policy.EXPENSIVE]
    assert sum(len(t) for t in tiers) == len(policy.CLASSIFIED), "a tool is in two tiers"


def test_every_engine_tool_is_classified_and_none_is_stale() -> None:
    assert len(NAMES) == 155
    assert sorted(NAMES - policy.CLASSIFIED) == [], "engine tools the policy has never heard of"
    assert sorted(policy.CLASSIFIED - NAMES) == [], "policy names that no longer exist"


def test_nothing_the_engine_calls_mutating_is_classified_read_only() -> None:
    wrongly_free = [
        t["name"] for t in SNAPSHOT
        if not t["readOnly"] and policy.classify(t["name"]) == "read"
        and t["name"] not in READ_ONLY_EXCEPTIONS
    ]
    assert wrongly_free == []


def test_every_read_only_engine_tool_flows_at_every_level() -> None:
    for t in SNAPSHOT:
        if t["readOnly"]:
            for level in policy.AUTONOMY_LEVELS:
                assert not _gated(t["name"], level), f"{t['name']} gated at {level}"


def test_engine_cheap_tools_follow_the_engines_own_tier() -> None:
    """A tool the engine treats as a cheap edit is not harder to run here, unless it
    executes code (the deliberate exceptions)."""
    stricter_on_purpose = {"run_plugin_action", "upload_plugin_file", "set_tab_module_enabled", "transform_series"}
    for t in SNAPSHOT:
        if t["engineCheap"] is True and t["name"] not in stricter_on_purpose:
            assert not _gated(t["name"], "guided"), f"{t['name']} asks under guided"
            assert _gated(t["name"], "manual") or policy.classify(t["name"]) == "always"


def test_every_engine_expensive_tool_asks_under_guided() -> None:
    for t in SNAPSHOT:
        if t["engineCheap"] is False:
            assert _gated(t["name"], "guided"), f"{t['name']} flows under guided"


def test_auto_asks_only_about_the_point_of_no_return() -> None:
    asked = {n for n in NAMES if _gated(n, "auto")}
    assert asked == policy.ALWAYS_GATED
    for must in ("submit_solve", "submit_scenario", "submit_scenarios", "clear_session",
                 "delete_run", "delete_scenario", "install_plugin", "run_plugin_action",
                 "set_import_secret", "install_tab_module", "siting_scan"):
        assert must in asked


def test_bypass_asks_about_nothing_manual_asks_about_every_mutation() -> None:
    free = policy.READ_ONLY | policy.EXPORTS
    for n in NAMES:
        assert not _gated(n, "bypass")
        assert _gated(n, "manual") == (n not in free)


def test_cancel_solve_is_never_blocked() -> None:
    for level in policy.AUTONOMY_LEVELS:
        assert not _gated("cancel_solve", level)


def test_an_unclassified_tool_asks_everywhere_except_bypass() -> None:
    assert policy.classify("brand_new_tool") == "unclassified"
    for level in ("manual", "guided", "auto"):
        assert _gated("brand_new_tool", level)
    assert not _gated("brand_new_tool", "bypass")


def test_bifrost_interaction_tools_and_prefixing() -> None:
    assert not policy.requires_approval("mcp__bifrost__ask_user", "manual")
    assert policy.short_tool_name("mcp__ragnarok__submit_solve") == "submit_solve"
    assert policy.requires_approval("mcp__ragnarok__submit_solve", "auto")


def test_an_export_into_the_workdir_flows_and_one_outside_asks(tmp_path: Path) -> None:
    work = tmp_path / "workdir"
    work.mkdir()
    assert not _gated("export_model", "auto", {"format": "netcdf"}, str(work))
    assert not _gated("export_model", "auto", {"dest": "out/model.nc"}, str(work))
    assert not _gated("export_run", "auto", {"dest": str(work / "run.zip")}, str(work))
    assert _gated("export_model", "auto", {"dest": "/etc/cron.d/x"}, str(work))
    assert _gated("export_workbook", "guided", {"dest": "../escape.xlsx"}, str(work))
    assert _gated("export_lp", "auto", {"file_name": str(tmp_path / "elsewhere.lp")}, str(work))
    assert not _gated("export_model", "bypass", {"dest": "/anywhere"}, str(work))


def test_policy_table_has_one_row_per_tool_and_agrees_with_requires_approval() -> None:
    table = catalog.policy_table()
    assert len(table) == len(NAMES)
    row = next(r for r in table if r["name"] == "submit_solve")
    assert row["tier"] == "always" and row["asks"] == {
        "manual": True, "guided": True, "auto": True, "bypass": False,
    }


def test_drift_reports_unclassified_and_stale() -> None:
    fake = [{"name": "submit_solve", "readOnly": False}, {"name": "brand_new_tool", "readOnly": False}]
    report = catalog.drift(fake)
    assert report["unclassified"] == ["brand_new_tool"]
    assert "get_kpis" in report["stale"]


def test_the_live_engine_catalogue_matches_the_policy() -> None:
    """Against the real checkout, not the snapshot: the one that goes stale first."""
    from bifrost import engine

    interpreter = engine.find_interpreter(RAGNAROK_ROOT)
    if interpreter is None or not (RAGNAROK_ROOT / "backend" / "mcp" / "server.py").exists():
        pytest.skip(f"No Ragnarok checkout with an environment at {RAGNAROK_ROOT}")
    try:
        tools = catalog.list_engine_tools(RAGNAROK_ROOT, interpreter)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    report = catalog.drift(tools)
    assert report == {"unclassified": [], "stale": [], "readOnlyMismatch": []}
    assert {t["name"] for t in tools} == NAMES, "refresh tests/fixtures/ragnarok_tools.json"
