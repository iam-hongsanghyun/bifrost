"""Tool-approval policy for the embedded agent.

Mirrors the MCP server's cheap/GATE classification (``backend/mcp/server.py``)
but enforcement moves from the model-facing ``confirm=true`` dance to the SDK's
``can_use_tool`` callback → a GUI approval card. The MCP subprocess itself runs
with ``RAGNAROK_MCP_AUTONOMY=auto`` so tools are never double-gated; external
MCP clients keep the env-var gate unchanged.

Autonomy levels (user-selected per conversation):

* ``manual`` — every mutating tool needs approval; read-only flows freely.
* ``guided`` (default) — cheap in-session edits flow; expensive actions
  (imports, transforms, solves, deletions) need approval.
* ``auto``   — only the point-of-no-return actions need approval:
  ``submit_solve`` (minutes of compute), ``clear_session`` (destroys the
  model), and the live-network importers.
* ``bypass`` — nothing is gated; the agent has full model control
  (imports, solves, clears included). The journal + undo remain the safety
  net, and the agent still asks decision QUESTIONS (ask_user) at major
  steps — bypass removes permission friction, not judgment.
"""
from __future__ import annotations

MCP_PREFIX = "mcp__ragnarok__"
# The in-process Bifrost helper server (ask_user) — interaction tools, never gated.
BIFROST_PREFIX = "mcp__bifrost__"

Autonomy = str  # 'manual' | 'guided' | 'auto' | 'bypass'
AUTONOMY_LEVELS = ("manual", "guided", "auto", "bypass")

# Read-only tools — never gated at any autonomy level.
READ_ONLY = {
    "list_importers", "source_health", "get_world_state", "get_sheet_page",
    "derive_series", "list_runs", "get_analytics", "get_derived", "get_queue",
    "list_components", "describe_component", "describe_run_options",
    "list_plugins", "describe_analytics", "physical_risk_libraries",
    "physical_risk_get_portfolio", "physical_risk_get_run",
    "physical_risk_transition", "physical_risk_finance", "physical_risk_report",
    # Deep reads + pre-flight added with the coverage-audit tool expansion.
    "validate_model", "get_sheet_stats", "get_series_window", "get_run_series",
    "get_journal", "get_journal_diff", "get_master_meta",
    "run_plugin_analysis", "optimize_procurement",
    # File exports write a local artefact but never touch the model — safe to run.
    "export_run", "export_model",
    # A solve cancel is a SAFETY action: always allowed so the agent can stop a
    # job it started, at any autonomy (never blocked behind an approval card).
    "cancel_solve",
}

# Cheap in-session edits — flow under guided/auto, gated under manual.
CHEAP_EDITS = {
    "edit_sheet", "add_bus", "add_generator", "add_load", "add_line",
    "add_storage", "add_component", "set_component", "set_snapshots",
    # Small, reversible management actions.
    "undo_journal_entry", "delete_queue_item", "set_queue_concurrency",
    "rename_run",
}

# Actions that always need approval, even at full autonomy.
ALWAYS_GATED = {
    "submit_solve", "clear_session", "import_dataset", "one_click_model",
    "build_starter_pack",
    # Permanent deletion of a stored run.
    "delete_run",
}

# Everything else that mutates (transforms, forecasts, clustering, removals,
# physical-risk runs) is "expensive": gated under manual AND guided.
EXPENSIVE = {
    "retarget_snapshots", "forecast_demand", "driver_forecast",
    "ev_reshape_demand", "cluster_network", "adjust_carrier_capacity",
    "transform_series", "remove_component", "attach_renewable_profiles",
    "attach_hydro_inflow", "physical_risk_seed", "physical_risk_set_scenario",
    "physical_risk_run",
    # Bulk edit, model swaps, plugin mutations, bulk rollback, re-solve.
    "forge_query", "promote_run", "derive_from_master", "run_plugin_transform",
    "run_plugin_contribute", "revert_session", "rerun_queue_item",
    # File imports overwrite the working model / write History — swap-like.
    "import_network", "import_project",
}


def short_tool_name(tool_name: str) -> str:
    for prefix in (MCP_PREFIX, BIFROST_PREFIX):
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return tool_name


def requires_approval(tool_name: str, autonomy: Autonomy) -> bool:
    """Whether this tool call must pause for a GUI approval card."""
    if tool_name.startswith(BIFROST_PREFIX):
        return False  # interaction tools (ask_user) ARE the human loop
    if autonomy == "bypass":
        return False  # full control — the journal + undo are the safety net
    name = short_tool_name(tool_name)
    if name in READ_ONLY:
        return False
    if name in ALWAYS_GATED:
        return True
    if autonomy == "manual":
        return True  # every mutation, cheap or not
    if autonomy == "guided":
        return name in EXPENSIVE or name not in CHEAP_EDITS
    # auto — only the ALWAYS_GATED set (handled above); unknown tools flow.
    return False


def normalize_autonomy(value: str | None) -> Autonomy:
    v = (value or "").strip().lower()
    return v if v in AUTONOMY_LEVELS else "guided"
