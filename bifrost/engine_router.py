"""``/api/engine/*`` — pick a Ragnarok, start it, stop it.

This is what makes Bifrost a wrapper rather than a client that assumes someone
already started the engine: the user points it at a checkout once, and Bifrost
remembers, launches and supervises it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import engine
from .config import CONFIG

router = APIRouter(prefix="/api/engine", tags=["engine"])


class RootPayload(BaseModel):
    path: str


@router.get("")
def get_engine() -> dict[str, object]:
    """Everything the setup screen needs in one call.

    ``configured`` is the question the UI actually asks — a remembered root that has
    since been moved or deleted reads as *not* configured, so the picker comes back
    instead of a connection error the user cannot act on.
    """
    root = engine.load_root()
    check = engine.check_root(str(root)) if root else None
    return {
        "configured": root is not None,
        "root": str(root) if root else None,
        "check": check.to_dict() if check else None,
        "apiBase": CONFIG.ragnarok_api_base,
        "process": engine.ENGINE.status(),
        "bifrostPython": engine.python_hint(),
        "suggestions": _suggest_roots(),
    }


@router.post("/check")
def check(payload: RootPayload) -> dict[str, object]:
    """Validate a directory WITHOUT saving it, so the picker can say why."""
    return engine.check_root(payload.path).to_dict()


@router.put("/root")
def set_root(payload: RootPayload) -> dict[str, object]:
    """Remember a Ragnarok checkout.

    Refused unless it looks like one. Accepting anything and failing later at the
    first API call would surface the error far from the choice that caused it.
    """
    result = engine.check_root(payload.path)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.reason)
    engine.save_root(Path(result.path))
    return {"saved": True, **get_engine()}


@router.post("/start")
def start() -> dict[str, object]:
    root = engine.load_root()
    if root is None:
        raise HTTPException(
            status_code=400, detail="No Ragnarok directory chosen yet."
        )
    try:
        return engine.ENGINE.start(root)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stop")
def stop() -> dict[str, object]:
    return engine.ENGINE.stop()


@router.get("/log")
def log(lines: int = 60) -> dict[str, object]:
    """The engine's own output. A failed start has to be diagnosable here, not
    only in a terminal the user never opened."""
    return {"lines": engine.ENGINE.tail_log(lines)}


def _suggest_roots() -> list[dict[str, object]]:
    """Likely Ragnarok checkouts near the user, so setup is usually one click.

    Only siblings of Bifrost and the obvious ``~/github`` layout are probed — this
    is a convenience, not a filesystem search, and scanning the home directory to
    save a paste would be a poor trade.
    """
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    candidates = [
        CONFIG.repo_root.parent / "ragnarok",
        CONFIG.repo_root.parent / "project_bifrost",
        Path.home() / "github" / "ragnarok",
    ]
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        result = engine.check_root(key)
        if result.valid:
            out.append(result.to_dict())
    return out
