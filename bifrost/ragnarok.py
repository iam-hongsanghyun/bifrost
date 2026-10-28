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
        headers: dict[str, str] | None = None,
        expect_bytes: bool = False,
    ) -> Any:
        url = f"{self.base}{path}"
        try:
            async with self._client() as client:
                resp = await client.request(
                    method, url, json=json, params=params, files=files, headers=headers
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

    async def journal(
        self, limit: int | None = None, before: int | None = None
    ) -> dict[str, Any]:
        """The mutation timeline. Bifrost does not keep its own — Ragnarok
        journals every mutation at the store facade, whoever made it.

        ``before`` pages backwards: entries with an id below it.
        """
        params: dict[str, Any] = {"session_id": self.session_id}
        if limit is not None:
            params["limit"] = limit
        if before is not None:
            params["before"] = before
        return await self._request("GET", "/api/session/journal", params=params)

    async def journal_diff(self, entry_id: int) -> dict[str, Any]:
        """The detailed (capped) diff of one journal entry."""
        return await self._request(
            "GET",
            f"/api/session/journal/{int(entry_id)}/diff",
            params={"session_id": self.session_id},
        )

    # ── reads that back the State panel ───────────────────────────────────────
    # Each is one Ragnarok route, passed through unshaped; the shaping lives in
    # ``bifrost.state`` so it can be tested without a transport.
    async def model_static(self) -> dict[str, Any]:
        """The working model's STATIC sheets ``{sheet: rows}`` (series omitted).

        ``{"model": null}`` when nothing is loaded.
        """
        return await self._request(
            "GET",
            "/api/session/model/full",
            params={"session_id": self.session_id, "staticOnly": "true"},
        )

    async def sheet(
        self, name: str, offset: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """One page of a session sheet (static or series)."""
        params: dict[str, Any] = {"session_id": self.session_id, "offset": offset}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", f"/api/session/sheet/{name}", params=params)

    async def queue(self) -> dict[str, Any]:
        """The solve queue: ``{jobs, concurrency, maxConcurrency, cpuCount}``."""
        return await self._request("GET", "/api/queue")

    async def enqueue(
        self, scenario: dict[str, Any] | None = None, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Queue a solve of the session's model. Ragnarok snapshots the model now."""
        return await self._request(
            "POST",
            "/api/queue",
            json={
                "sessionId": self.session_id,
                "scenario": scenario or {},
                "options": options or {},
            },
        )

    async def cancel_job(self, item_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/queue/{item_id}/cancel")

    async def runs(self) -> dict[str, Any]:
        """Every stored run's lightweight meta, newest first."""
        return await self._request("GET", "/api/runs")

    async def run_analytics(self, name: str) -> dict[str, Any]:
        """A run's analytics bundle (no input model, no raw series)."""
        return await self._request("GET", f"/api/runs/{name}/analytics")

    async def run_kpis(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/runs/{name}/kpis")

    async def run_series(
        self, name: str, sheet: str, *, start: int = 0, end: int | None = None,
        columns: list[str] | None = None, max_points: int | None = None,
    ) -> dict[str, Any]:
        """A windowed (and optionally downsampled) slice of one output sheet."""
        params: dict[str, Any] = {"start": start}
        if end is not None:
            params["end"] = end
        if columns:
            params["columns"] = ",".join(columns)
        if max_points is not None:
            params["maxPoints"] = max_points
        return await self._request("GET", f"/api/runs/{name}/series/{sheet}", params=params)

    async def run_model_sheet(
        self, name: str, sheet: str, offset: int = 0, limit: int = 5000
    ) -> dict[str, Any]:
        """A sheet of the model a run was SOLVED on (not the session's current one)."""
        return await self._request(
            "GET",
            f"/api/runs/{name}/model/sheet/{sheet}",
            params={"offset": offset, "limit": limit},
        )

    async def compare_runs(
        self, names: list[str], topics: list[str] | None = None
    ) -> dict[str, Any]:
        """Ragnarok's comparison matrix data for 2 to 5 runs."""
        params: dict[str, Any] = {"runs": ",".join(names)}
        if topics:
            params["topics"] = ",".join(topics)
        return await self._request("GET", "/api/results/compare", params=params)

    # ── project packages: a conversation's whole world ────────────────────────
    async def export_project(
        self, *, scenario: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        conversation: dict[str, Any] | None = None,
    ) -> bytes:
        """The session's model as a project ``.zip``.

        Ragnarok resolves the model from ``sessionId`` server-side and hydrates
        the time-series into it, so the bytes are complete even though Bifrost
        never held the series. ``conversation`` (``{events, model, autonomy, ...}``)
        is embedded as the package's chat sidecar: Ragnarok keeps no conversation
        store, so whoever owns the chat has to hand it over. ``None`` omits the
        sidecar (and an older Ragnarok that does not know the field ignores it).
        """
        body: dict[str, Any] = {
            "sessionId": self.session_id,
            "scenario": scenario or {},
            "options": options or {},
            "result": result or {},
        }
        if conversation is not None:
            body["conversation"] = conversation
        return await self._request(
            "POST", "/api/export/project", json=body, expect_bytes=True,
        )

    async def import_project(self, data: bytes, filename: str) -> dict[str, Any]:
        """Parse a project ``.zip`` and return its bundle.

        ``bundle["conversation"]`` is the package's chat sidecar (``None`` without
        one) on a Ragnarok that surfaces it; an older one omits the key entirely.

        This is what makes switching conversations whole: the package carries the
        model, its data, the solved result and the chat, so restoring one restores
        the world it was produced in.
        """
        return await self._request(
            "POST",
            "/api/import/project/load",
            files={"file": (filename, data, "application/zip")},
        )

    async def load_model(
        self, model: dict[str, Any], filename: str = "", scenario_name: str = ""
    ) -> dict[str, Any]:
        """Put a model into the shared session, replacing whatever is there.

        ``/api/import/project/load`` only PARSES a package and returns the bundle —
        "opening a file" in Ragnarok is the browser's job, and nothing is stored
        server-side. Bifrost has no browser in that loop, so without this call the
        session stays exactly as it was and a conversation "restored" its model
        into a bundle nobody held.
        """
        return await self._request(
            "POST",
            "/api/session/model",
            json={
                "sessionId": self.session_id,
                "model": model,
                "filename": filename,
                "scenarioName": scenario_name,
            },
        )

    async def open_project(self, data: bytes, filename: str) -> dict[str, Any]:
        """Parse a project ``.zip`` AND make it the session's working model.

        The bundle is returned for the caller's own use (result, scenario, chat).
        A package with no model clears the session instead of leaving the previous
        one in place, for the reason ``/activate`` documents.
        """
        bundle = await self.import_project(data, filename)
        model = bundle.get("model") or {}
        if model:
            await self.load_model(model, filename=bundle.get("filename") or filename)
        else:
            await self.clear_session()
        return bundle

    async def clear_session(self) -> Any:
        """Drop the session's model. Used when a conversation with no project yet
        becomes active, so it cannot inherit the previous one's model.

        The session id travels as a QUERY parameter, which is how Ragnarok reads
        it. It used to be sent in a JSON body that Ragnarok ignores, so the call
        cleared the ``default`` session whatever id was configured — correct by
        accident for the default, destructive to the wrong session otherwise.
        """
        return await self._request(
            "POST", "/api/session/clear", params={"session_id": self.session_id}
        )

    # ── the data-ask loop (request_data) ──────────────────────────────────────
    async def config(self) -> dict[str, Any]:
        """Ragnarok's boot bundle; its ``schema.components`` is the live schema
        a data request is validated against (never a copy kept here)."""
        return await self._request("GET", "/api/config")

    async def merge_fragment(
        self, fragment: dict[str, Any], *, conversation_id: str | None = None
    ) -> dict[str, Any]:
        """Merge a fragment into the session model (``apply``), as the USER.

        No ``X-Ragnarok-Actor`` header is sent, so the journal tags the entry
        ``user``: the person pasted these rows, the agent did not write them. The
        conversation id still travels so the change links to its chat step.
        """
        headers = {"X-Ragnarok-Conversation": conversation_id} if conversation_id else None
        return await self._request(
            "POST", "/api/session/merge-fragment",
            json={"sessionId": self.session_id, "fragment": fragment, "apply": True},
            headers=headers,
        )

    async def identity(self) -> dict[str, Any]:
        """Which checkout is serving, and where its data lives."""
        return await self._request("GET", "/api/identity")


CLIENT = RagnarokClient()
