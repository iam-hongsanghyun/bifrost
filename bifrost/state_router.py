"""``/api/state/*`` — the live view of the model and its runs, for the State panel.

Thin by design. Each route is one or two fetches against Ragnarok through
:data:`bifrost.ragnarok.CLIENT` (the browser never calls the engine for these) and a
call into :mod:`bifrost.state`, which does the shaping and is tested without a
transport. What changes the model is NOT here: the agent edits through Ragnarok's
tools and the journal records it; the only writes in this module are the two a Runs
panel needs — queue a solve, cancel one.

Ragnarok's own words are passed through on failure (its errors are written to be
acted on), with its status where there is one and 503 when it cannot be reached.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import state
from .config import CONFIG
from .ragnarok import CLIENT, RagnarokError, RagnarokUnavailable

router = APIRouter(prefix="/api/state", tags=["state"])

_MAX_COMPARE_RUNS = 5


async def _call(awaitable: Any) -> Any:
    """Await a Ragnarok call; turn its two failure modes into HTTP errors."""
    try:
        return await awaitable
    except RagnarokUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RagnarokError as exc:
        detail = exc.detail
        try:  # FastAPI bodies are {"detail": "..."}; surface the sentence, not the JSON.
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
                detail = parsed["detail"]
        except ValueError:
            pass
        raise HTTPException(status_code=exc.status, detail=detail) from exc


async def _optional(awaitable: Any) -> Any:
    """Like ``_call`` but a missing thing (404) is ``None`` — a run with no
    storage sheet is normal, not an error."""
    try:
        return await _call(awaitable)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


# ── the model ────────────────────────────────────────────────────────────────


@router.get("/model")
async def get_model() -> dict[str, Any]:
    """Counts by type, carriers and the topology graph of the session's model."""
    meta, full = await asyncio.gather(_call(CLIENT.session_meta()), _call(CLIENT.model_static()))
    return state.shape_model(meta or {}, (full or {}).get("model"))


@router.get("/sheet/{name}")
async def get_sheet(
    name: str,
    q: str | None = None,
    sort: str | None = None,
    desc: bool = False,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """One sheet of the session's model, filtered (``q``), sorted and paged here."""
    sheet = await _call(CLIENT.sheet(name, 0, CONFIG.table_max_rows))
    return state.table_page(sheet, offset=offset, limit=limit, q=q, sort=sort, descending=desc)


# ── the journal ──────────────────────────────────────────────────────────────


@router.get("/journal")
async def get_journal(
    limit: int = Query(50, ge=1, le=500), before: int | None = Query(None, ge=1)
) -> dict[str, Any]:
    """Newest-first mutation timeline; ``next_before`` pages back."""
    return state.shape_journal(await _call(CLIENT.journal(limit=limit, before=before)), limit)


@router.get("/journal/{entry_id}/diff")
async def get_journal_diff(entry_id: int) -> dict[str, Any]:
    return await _call(CLIENT.journal_diff(entry_id))


# ── the run queue and stored runs ────────────────────────────────────────────


@router.get("/queue")
async def get_queue() -> dict[str, Any]:
    return state.shape_queue(await _call(CLIENT.queue()))


class SolveRequest(BaseModel):
    scenario: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/queue")
async def queue_solve(payload: SolveRequest) -> dict[str, Any]:
    """Queue a solve of the session's model (Ragnarok snapshots it at submit time)."""
    return await _call(CLIENT.enqueue(payload.scenario, payload.options))


@router.post("/queue/{item_id}/cancel")
async def cancel_solve(item_id: str) -> dict[str, Any]:
    return await _call(CLIENT.cancel_job(item_id))


@router.get("/runs")
async def get_runs() -> dict[str, Any]:
    return state.shape_runs(await _call(CLIENT.runs()))


@router.get("/runs/{name}/dashboard")
async def get_dashboard(
    name: str, bus: str | None = None, carrier: str | None = None, period: str | None = None
) -> dict[str, Any]:
    """Columnar results for one run, narrowed by bus / carrier / period."""
    analytics, kpis, generators, storage_units = await asyncio.gather(
        _call(CLIENT.run_analytics(name)),
        _call(CLIENT.run_kpis(name)),
        _optional(CLIENT.run_model_sheet(name, "generators")),
        _optional(CLIENT.run_model_sheet(name, "storage_units")),
    )
    generator_p = None
    if bus:
        count = int(((analytics.get("result") or {}).get("runMeta") or {}).get("snapshotCount") or 0)
        generator_p = await _optional(
            CLIENT.run_series(name, "generators-p", max_points=max(count, 1))
        )
    return state.build_dashboard(
        name=name, analytics=analytics, kpis=(kpis or {}).get("kpis") or {},
        generators=(generators or {}).get("rows") or [],
        storage_units=(storage_units or {}).get("rows") or [],
        generator_p=generator_p, bus=bus or None, carrier=carrier or None, period=period or None,
    )


@router.get("/runs/{name}/snapshot")
async def get_snapshot(
    name: str, index: int = Query(..., ge=0), bus: str | None = None
) -> dict[str, Any]:
    """Dispatch by carrier, asset and bus at ONE snapshot of a run — what clicking a
    time point on a chart opens."""
    window = {"start": index, "end": index + 1, "max_points": 1}
    analytics, generators, gen_p, load_p, price, flows = await asyncio.gather(
        _call(CLIENT.run_analytics(name)),
        _optional(CLIENT.run_model_sheet(name, "generators")),
        _call(CLIENT.run_series(name, "generators-p", **window)),
        _optional(CLIENT.run_series(name, "loads-p", **window)),
        _optional(CLIENT.run_series(name, "buses-marginal_price", **window)),
        _optional(CLIENT.run_series(name, "lines-p0", **window)),
    )
    result = analytics.get("result") or {}
    colours = {str(m.get("label")): str(m["color"])
               for m in (result.get("carrierMix") or []) if m.get("color")}
    rows = gen_p.get("rows") or []
    label = state._ts(rows[0].get(gen_p.get("indexCol", "snapshot"))) if rows else ""
    return state.build_snapshot(
        index=index, label=label, generator_p=gen_p, load_p=load_p, price=price, flows=flows,
        generators=(generators or {}).get("rows") or [], colours=colours, bus=bus or None,
    )


@router.get("/compare")
async def compare(runs: str = Query(..., description="2 to 5 run names, comma-separated.")) -> dict[str, Any]:
    """Runs side by side: KPIs with deltas against the first, mix, capacity, cost, price."""
    names = [n.strip() for n in runs.split(",") if n.strip()]
    if not 2 <= len(names) <= _MAX_COMPARE_RUNS:
        raise HTTPException(status_code=400, detail=f"Give 2 to {_MAX_COMPARE_RUNS} run names.")
    fetched = await asyncio.gather(
        *[_call(CLIENT.run_analytics(n)) for n in names],
        *[_call(CLIENT.run_kpis(n)) for n in names],
    )
    entries = [
        {"name": n, "analytics": fetched[i], "kpis": (fetched[len(names) + i] or {}).get("kpis") or {}}
        for i, n in enumerate(names)
    ]
    return state.build_comparison(entries)
