"""Tool-approval policy for the embedded agent.

Mirrors the MCP server's cheap/GATE classification (``backend/mcp/server.py``,
``tools_*.py``: a tool calling ``_needs_confirm(cheap=True)`` is a CHEAP_EDIT,
``cheap=False`` is EXPENSIVE) but enforcement moves from the model-facing
``confirm=true`` dance to the SDK's ``can_use_tool`` callback → a GUI approval card.

The two gates must never stack and never cancel. The MCP subprocess is launched
with ``RAGNAROK_MCP_AUTONOMY=auto``, which turns Ragnarok's own ``confirm=true``
gate OFF for every tool, so a call the user has approved here runs at once instead
of coming back as a "preview" the agent must re-send. Ragnarok's gate therefore
never fires, and THIS module is the only gate: every one of the engine's tools must
be classified below, and an unclassified tool is treated as needing approval
(``tests/test_policy.py`` fails the day the engine's catalogue outgrows this file).
External MCP clients keep the env-var gate unchanged.

Autonomy levels (user-selected per conversation):

* ``manual`` — every mutating tool needs approval; read-only flows freely.
* ``guided`` (default) — cheap in-session edits flow; expensive actions
  (imports, transforms, solves, deletions) need approval.
* ``auto``   — only the point-of-no-return actions need approval: solves
  (minutes of compute), ``clear_session`` (destroys the model), the live-network
  importers, permanent deletions, and anything that installs or executes code or
  touches stored credentials.
* ``bypass`` — nothing is gated; the agent has full model control
  (imports, solves, clears included). The journal + undo remain the safety
  net, and the agent still asks decision QUESTIONS (ask_user) at major
  steps — bypass removes permission friction, not judgment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MCP_PREFIX = "mcp__ragnarok__"
# The in-process Bifrost helper server (ask_user) — interaction tools, never gated.
BIFROST_PREFIX = "mcp__bifrost__"

Autonomy = str  # 'manual' | 'guided' | 'auto' | 'bypass'
AUTONOMY_LEVELS = ("manual", "guided", "auto", "bypass")

# Read-only tools — never gated at any autonomy level.
READ_ONLY = {
    # Introspection, pre-flight and results (annotated read-only by the engine).
    "list_importers", "source_health", "get_world_state", "get_sheet_page",
    "derive_series", "list_runs", "get_analytics", "get_derived", "get_queue",
    "list_components", "describe_component", "describe_run_options",
    "list_plugins", "describe_analytics", "physical_risk_libraries",
    "physical_risk_get_portfolio", "physical_risk_get_run", "physical_risk_report",
    "validate_model", "check_model", "get_sheet_stats", "get_series_window",
    "get_run_series", "get_journal", "get_journal_diff", "get_master_meta",
    "run_plugin_analysis", "optimize_procurement",
    "get_server_info", "get_server_log", "list_examples", "list_import_databases",
    "list_starter_packs", "list_import_countries", "get_import_provenance",
    "list_import_secrets", "get_distinct_values", "get_run_model_sheet",
    "get_plugin", "refresh_plugins", "list_plugin_files", "get_plugin_options",
    "list_tab_modules", "pypsa_earth_status", "pypsa_earth_build_status",
    "list_scenarios", "describe_scenario", "list_report_perspectives", "get_report",
    "compare_runs", "compare_run_statistics", "get_metric_options", "get_period_results", "get_run_analysis", "get_kpis", "compare_companies", "get_asset_details",
    "get_statistics", "network_analysis", "consistency_check",
    "list_demand_db", "get_demand_db_slice",
    # Siting screening statistics, P2X and plant reads.
    "list_siting_datasets", "get_siting_dataset", "validate_siting_dataset",
    "siting_correlation", "siting_lagged_correlation", "siting_profiles",
    "siting_portfolio_metrics", "siting_optimise_portfolio", "siting_cluster_sites",
    "siting_region_series", "siting_region_structure",
    "get_carbon_price", "get_annual_budgets",
    "list_p2x_technologies", "preview_p2x_chain", "get_p2x_results",
    "list_plant_types", "get_plant_results",
    "list_conversions", "pypsa_io_capabilities", "describe_piecewise",
    "get_piecewise_curve", "check_piecewise_options", "describe_optimise_options",
    # The engine annotates these two as writing (they store a result in the
    # physical-risk session) but they never touch the working model.
    "physical_risk_transition", "physical_risk_finance",
    # A solve cancel is a SAFETY action: always allowed so the agent can stop a
    # job it started, at any autonomy (never blocked behind an approval card).
    "cancel_solve",
}

# File exports write a local artefact but never touch the model — safe to run, as
# long as the artefact lands in the agent's own working directory. A destination
# outside it (an absolute path, or one that climbs out with ``..``) is the agent
# writing wherever it likes on the user's disk, and needs approval.
EXPORTS = {
    "export_run", "export_model", "export_workbook", "export_pypsa_native", "export_lp",
}
#: Tool-input keys naming where an export lands.
EXPORT_PATH_KEYS = ("dest", "file_name")

# Cheap in-session edits — flow under guided/auto, gated under manual.
CHEAP_EDITS = {
    "edit_sheet", "add_bus", "add_generator", "add_load", "add_line",
    "add_storage", "add_component", "set_component", "set_snapshots",
    # Small, reversible management actions.
    "undo_journal_entry", "delete_queue_item", "set_queue_concurrency",
    "rename_run", "reload_config",
    # Scenario authoring and per-model settings (the engine's own cheap tier).
    "save_scenario", "set_active_scenario", "set_custom_dsl",
    "set_piecewise_curve", "clear_piecewise_curve", "set_carrier_growth_limit",
    "set_maintenance",
    # Carbon price and annual budgets stored as settings (the engine gates them as cheap).
    "set_carbon_price", "set_annual_budget",
    # Configuring / stopping a PyPSA-Earth build — the build itself is gated.
    "pypsa_earth_configure", "pypsa_earth_stop",
}

# Actions that always need approval, even at full autonomy.
ALWAYS_GATED = {
    # Minutes of compute.
    "submit_solve", "submit_scenario", "submit_scenarios", "rerun_queue_item",
    "pypsa_earth_build",
    # Destroys or replaces the model wholesale / live-network importers.
    "clear_session", "import_dataset", "one_click_model", "build_starter_pack",
    "siting_scan",
    # Permanent deletion.
    "delete_run", "delete_scenario", "delete_plugin_file", "uninstall_plugin",
    "remove_tab_module", "delete_siting_dataset",
    # Installs or runs code, or touches stored credentials. Tab modules and plugins
    # change the app for every session on this engine and survive restarts.
    "install_plugin", "upload_plugin_file", "run_plugin_action",
    "install_tab_module", "install_tab_module_zip",
    "set_import_secret", "delete_import_secret",
}

# Everything else that mutates (transforms, forecasts, clustering, removals, model
# swaps, physical-risk runs) is "expensive": gated under manual AND guided.
EXPENSIVE = {
    "retarget_snapshots", "forecast_demand", "driver_forecast",
    "ev_reshape_demand", "cluster_network", "adjust_carrier_capacity",
    "transform_series", "remove_component", "attach_renewable_profiles",
    "attach_hydro_inflow", "physical_risk_seed", "physical_risk_set_scenario",
    "physical_risk_run",
    # Bulk edit, model swaps, plugin mutations, bulk rollback.
    "forge_query", "promote_run", "derive_from_master", "run_plugin_transform",
    "run_plugin_contribute", "revert_session", "adjust_values",
    "revert_adjustments", "apply_seasonal_rating", "snap_to_buses",
    "add_conversion", "set_tab_module_enabled", "pypsa_earth_apply",
    # Plants and chains add or replace several components at once.
    "add_plant", "edit_plant", "remove_plant", "build_p2x_chain",
    # Writes horizon budgets into the model's component rows.
    "apply_annual_budgets",
    # Screening: an upload stores a dataset; a promotion adds generators.
    "upload_siting_dataset", "promote_siting_sites",
    # Reducing / slicing / merging the model replaces it.
    "temporal_cluster", "slice_model", "merge_models", "merge_fragment",
    "cluster_network_hac",
    # File and queue imports overwrite the working model / write History.
    "import_network", "import_demand_db", "import_project", "import_pypsa_native", "import_master",
    "clear_master", "import_result", "load_example", "load_queue_item",
}

#: Every tool the policy knows by name, once. Disjoint by construction (tested).
CLASSIFIED = READ_ONLY | EXPORTS | CHEAP_EDITS | ALWAYS_GATED | EXPENSIVE


def short_tool_name(tool_name: str) -> str:
    for prefix in (MCP_PREFIX, BIFROST_PREFIX):
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return tool_name


def classify(tool_name: str) -> str:
    """The tier a tool belongs to: ``read`` | ``export`` | ``cheap`` | ``expensive`` |
    ``always`` | ``unclassified``."""
    name = short_tool_name(tool_name)
    if name in READ_ONLY:
        return "read"
    if name in EXPORTS:
        return "export"
    if name in ALWAYS_GATED:
        return "always"
    if name in CHEAP_EDITS:
        return "cheap"
    if name in EXPENSIVE:
        return "expensive"
    return "unclassified"


def export_leaves_workdir(tool_input: dict[str, Any] | None, workdir: str | Path | None) -> bool:
    """Whether an export names a destination outside the agent's working directory."""
    if not tool_input:
        return False
    base = Path(workdir).resolve() if workdir else None
    for key in EXPORT_PATH_KEYS:
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = (base or Path.cwd()) / target
        resolved = target.resolve()
        if base is None or not resolved.is_relative_to(base):
            return True
    return False


def requires_approval(
    tool_name: str,
    autonomy: Autonomy,
    tool_input: dict[str, Any] | None = None,
    workdir: str | Path | None = None,
) -> bool:
    """Whether this tool call must pause for a GUI approval card."""
    if tool_name.startswith(BIFROST_PREFIX):
        return False  # interaction tools (ask_user) ARE the human loop
    if autonomy == "bypass":
        return False  # full control — the journal + undo are the safety net
    tier = classify(tool_name)
    if tier == "read":
        return False
    if tier == "export":
        return export_leaves_workdir(tool_input, workdir)
    if tier in ("always", "unclassified"):
        # An unclassified tool is one this file has never heard of — a newer
        # engine's. Asking is the safe default; flowing is how a destructive tool
        # slips past an approval policy nobody updated.
        return True
    if autonomy == "manual":
        return True  # every mutation, cheap or not
    if autonomy == "guided":
        return tier == "expensive"
    return False  # auto — only the ALWAYS_GATED set (handled above)


def normalize_autonomy(value: str | None) -> Autonomy:
    v = (value or "").strip().lower()
    return v if v in AUTONOMY_LEVELS else "guided"
