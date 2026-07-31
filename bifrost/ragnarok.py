"""The Ragnarok client — Bifrost's only route to a model.

Every modelling capability lives in Ragnarok and is reached over HTTP. Bifrost
holds no PyPSA, no solver and no second copy of the workbook; that is the whole
point of the wrapper split, and it is what stopped the previous incarnation from
being a 136k-line fork that had to be re-fixed in two places.

Two consequences worth stating, because they shape everything else:

* **The session is shared.** The agent and the embedded Ragnarok UI act on the
  same ``session_id``, so a change the agent makes is a change the user is
  already looking at. Ragnarok journals every mutation through its store facade
  and publishes a ``session.version`` event, so "Ragnarok captures every change
  in the conversation" needs no cooperation from Bifrost at all — it falls out
  of pointing both surfaces at one session.
* **A project package is the unit of persistence.** Ragnarok's
  ``/api/export/project`` returns a ``.zip`` holding the canonical model JSON, a
  meta sidecar, a conversation sidecar and a readable xlsx; ``/api/import/project/load``
  reads it back. So a conversation's entire world — model, data, results and the
  chat that produced them — round-trips through one artefact Ragnarok already
  knows how to write.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import CONFIG

logger = logging.getLogger("bifrost.ragnarok")


class RagnarokUnavailable(RuntimeError):
    """Ragnarok is not reachable. Distinct from a 4xx: nothing was attempted."""


class RagnarokError(RuntimeError):
    """Ragnarok answered with an error. Carries its status and detail verbatim.

    The detail is surfaced to the user (and to the agent) unchanged — Ragnarok's
    messages are written to be actionable, and paraphrasing them loses that.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Ragnarok {status}: {detail}")
        self.status = status
        self.detail = detail


class RagnarokClient:
    """Thin async HTTP client. One instance per process is fine."""

    def __init__(
        self,
        base: str | None = None,
        session_id: str | None = None,
        transport: Any | None = None,
    ) -> None:
        self.base = (base or CONFIG.ragnarok_api_base).rstrip("/")
        self.session_id = session_id or CONFIG.ragnarok_session_id
        #: Optional httpx transport. Production leaves it None (real network); a
        #: test passes ``httpx.ASGITransport(app=ragnarok_app)`` to exercise the
        #: whole round-trip against the real engine with no server to start.
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=CONFIG.request_timeout_s, transport=self.transport
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        expect_bytes: bool = False,
    ) -> Any:
        url = f"{self.base}{path}"
        try:
            async with self._client() as client:
                resp = await client.request(
                    method, url, json=json, params=params, files=files
                )
        except httpx.RequestError as exc:
            raise RagnarokUnavailable(
                f"Cannot reach Ragnarok at {self.base} ({exc.__class__.__name__}). "
                "Start it, or set RAGNAROK_API_BASE."
            ) from exc
        if resp.status_code >= 400:
            raise RagnarokError(resp.status_code, resp.text.strip())
        if expect_bytes:
            return resp.content
        if not resp.content:
            return None
        return resp.json()

    # ── health ────────────────────────────────────────────────────────────────
    async def status(self) -> dict[str, Any]:
        """Ragnarok's readiness. Used to render a degraded state rather than a
        dead chat box when the engine is not up."""
        return await self._request("GET", "/api/status")

    # ── the shared session ────────────────────────────────────────────────────
    async def session_meta(self) -> dict[str, Any]:
        """Session meta, including the journal's monotonic ``version``.

        ``{}`` when nothing is loaded — that empty shape is Ragnarok's "is
        anything loaded?" contract, so it is passed through rather than
        normalised into a truthy object.
        """
        return await self._request(
            "GET", "/api/session/meta", params={"session_id": self.session_id}
        )

    async def journal(self, limit: int | None = None) -> dict[str, Any]:
        """The mutation timeline. Bifrost does not keep its own — Ragnarok
        journals every mutation at the store facade, whoever made it."""
        params: dict[str, Any] = {"session_id": self.session_id}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/api/journal", params=params)

    # ── project packages: a conversation's whole world ────────────────────────
    async def export_project(
        self, *, scenario: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> bytes:
        """The session's model as a project ``.zip``.

        Ragnarok resolves the model from ``sessionId`` server-side and hydrates
        the time-series into it, so the bytes are complete even though Bifrost
        never held the series.
        """
        return await self._request(
            "POST",
            "/api/export/project",
            json={
                "sessionId": self.session_id,
                "scenario": scenario or {},
                "options": options or {},
                "result": result or {},
            },
            expect_bytes=True,
        )

    async def import_project(self, data: bytes, filename: str) -> dict[str, Any]:
        """Load a project ``.zip`` into the session and return its bundle.

        This is what makes switching conversations whole: the package carries the
        model, its data, the solved result and the chat, so restoring one restores
        the world it was produced in.
        """
        return await self._request(
            "POST",
            "/api/import/project/load",
            files={"file": (filename, data, "application/zip")},
        )

    async def clear_session(self) -> Any:
        """Drop the session's model. Used when a conversation with no project yet
        becomes active, so it cannot inherit the previous one's model."""
        return await self._request(
            "POST", "/api/session/clear", json={"sessionId": self.session_id}
        )


CLIENT = RagnarokClient()
