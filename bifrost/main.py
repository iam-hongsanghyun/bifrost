"""Bifrost — FastAPI app. An AI-agentic IDE over a running Ragnarok.

Bifrost hosts three things and nothing else:

1. **the agent** (``/api/agent/*``) — a Claude Agent SDK session whose tools are
   Ragnarok's MCP surface, so anything the GUI can do the agent can do;
2. **conversations** (``/api/conversations/*``) — the left rail, and the project
   package each conversation owns;
3. **its own event stream** (``/api/events``) — chat tokens, tool starts/ends,
   approval requests. Distinct from Ragnarok's ``session.version`` stream, which
   the browser subscribes to directly.

Everything about a *model* — sheets, solves, results, imports, transforms — is
Ragnarok's, reached over HTTP. There is no PyPSA here, no solver, and no second
copy of the workbook. That is deliberate: the previous incarnation was a fork of
the whole engine, and the same bug then had to be fixed twice.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bifrost")

app = FastAPI(title="Bifrost", version="0.1.0")

# The dev frontend is served from another port, and the browser also talks to
# Ragnarok directly for the embedded UI and its event stream.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .agent import router as _agent_router  # noqa: E402
from .conversations import router as _conversations  # noqa: E402
from .conversations.router import router as _conversations_router  # noqa: E402

app.include_router(_agent_router.router)
app.include_router(_conversations_router)


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Bifrost's own readiness, plus whether its engine is reachable.

    Reported together on purpose: Bifrost being up is not interesting on its own,
    because without Ragnarok there is no model to talk about. The frontend renders
    a "start Ragnarok" state off this rather than letting the first chat message
    fail.
    """
    from .ragnarok import CLIENT, RagnarokError, RagnarokUnavailable

    engine: dict[str, object]
    try:
        status = await CLIENT.status()
        engine = {"reachable": True, "base": CLIENT.base, "status": status}
    except RagnarokUnavailable as exc:
        engine = {"reachable": False, "base": CLIENT.base, "error": str(exc)}
    except RagnarokError as exc:
        engine = {"reachable": True, "base": CLIENT.base, "error": exc.detail}
    return {
        "ok": True,
        "sessionId": CONFIG.ragnarok_session_id,
        "dataDir": str(CONFIG.data_dir),
        "engine": engine,
    }


@app.get("/api/config")
async def client_config() -> dict[str, object]:
    """What the frontend needs to know at boot.

    ``ragnarokBase`` is handed to the browser because the Ragnarok tab embeds the
    real Ragnarok UI and subscribes to its event stream directly — proxying either
    through Bifrost would add a hop and break SSE for no benefit.
    """
    return {
        "ragnarokBase": CONFIG.ragnarok_api_base,
        "sessionId": CONFIG.ragnarok_session_id,
    }
