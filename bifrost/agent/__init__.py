"""Embedded Bifrost agent (Claude Agent SDK host).

The SDK dependency is optional (``requirements-agent.txt``, like the MCP
server's ``requirements-mcp.txt``): the router always mounts, and
``GET /api/agent/status`` reports a degraded state when the SDK or the Claude
Code CLI runtime is missing — the Assistant tab renders setup instructions
instead of a dead chat box. Keeps the Mjolnir subtree inert.
"""
from __future__ import annotations

try:  # pragma: no cover — exercised via status endpoint tests
    import claude_agent_sdk  # noqa: F401

    AGENT_AVAILABLE = True
except Exception:  # noqa: BLE001 — any import failure means "not available"
    AGENT_AVAILABLE = False
