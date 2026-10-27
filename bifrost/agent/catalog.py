"""The engine's actual tool catalogue, read from the checkout that will serve it.

The approval policy (:mod:`.policy`) names tools; Ragnarok's MCP server decides
which tools exist. They drift: the server grew from 73 tools to 154 while the
policy stayed put, and the tools it did not name were silently allowed under
``auto``. Asking the engine's own interpreter for its catalogue is how the two are
compared — by ``GET /api/agent/tools?live=true`` for a human, and by
``tests/test_policy.py`` for CI.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import policy

# Run inside the ENGINE's interpreter (Bifrost's has no `mcp`, no PyPSA). It prints
# one JSON line; everything else on stdout (an import-time banner) is ignored.
_LIST_SCRIPT = """
import asyncio, json
from backend.mcp.server import mcp
from backend.mcp import tools_analysis, tools_components, tools_optimise, tools_piecewise, tools_runspec  # noqa: F401
async def main():
    out = []
    for t in await mcp.list_tools():
        a = t.annotations
        props = (t.inputSchema or {}).get("properties", {})
        out.append({"name": t.name, "readOnly": bool(a and a.readOnlyHint),
                    "destructive": getattr(a, "destructiveHint", None),
                    "hasConfirm": "confirm" in props})
    print(json.dumps(out))
asyncio.run(main())
"""


def list_engine_tools(root: Path, interpreter: str, timeout_s: float = 90.0) -> list[dict[str, Any]]:
    """Every tool the checkout's MCP server registers, with its annotations."""
    import os

    env = {**os.environ, "PYTHONPATH": str(root), "RAGNAROK_MCP_AUTOSTART": "0"}
    proc = subprocess.run(
        [interpreter, "-c", _LIST_SCRIPT],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout_s, check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("[")]
    if proc.returncode != 0 or not lines:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError("Could not list the engine's tools: " + " | ".join(tail))
    return json.loads(lines[-1])


def drift(tools: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Where the policy and the engine disagree.

    ``unclassified`` tools exist in the engine and not in the policy (they will ask
    for approval at every level but bypass); ``stale`` are named in the policy and no
    longer exist; ``readOnlyMismatch`` are classified read-only here though the
    engine does not annotate them so, which would let a mutation through unasked.
    """
    names = {t["name"] for t in tools}
    mismatch = sorted(
        t["name"] for t in tools
        if not t["readOnly"] and policy.classify(t["name"]) == "read"
        and t["name"] not in {"cancel_solve", "physical_risk_transition", "physical_risk_finance"}
    )
    return {
        "unclassified": sorted(n for n in names if n not in policy.CLASSIFIED),
        "stale": sorted(policy.CLASSIFIED - names),
        "readOnlyMismatch": mismatch,
    }


def policy_table() -> list[dict[str, Any]]:
    """Every classified tool and whether each autonomy level asks about it."""
    return [
        {
            "name": name,
            "tier": policy.classify(name),
            "asks": {
                level: policy.requires_approval(policy.MCP_PREFIX + name, level)
                for level in policy.AUTONOMY_LEVELS
            },
        }
        for name in sorted(policy.CLASSIFIED)
    ]
