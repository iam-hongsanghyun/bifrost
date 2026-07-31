"""``/api/conversations/*`` — the left rail, and switching worlds.

The endpoint that matters here is ``POST /{id}/activate``. Switching conversation
is not a UI state change: it loads that conversation's project package into the
shared Ragnarok session, so the Ragnarok tab, the agent and the transcript are all
describing the same model again. Everything else in this module is bookkeeping
around that.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from ..ragnarok import CLIENT, RagnarokError, RagnarokUnavailable
from .store import DEFAULT_GROUP, STORE

logger = logging.getLogger("bifrost.conversations.router")

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class CreatePayload(BaseModel):
    title: str = "New conversation"
    group: str = DEFAULT_GROUP


class UpdatePayload(BaseModel):
    title: str | None = None
    group: str | None = None


class EventsPayload(BaseModel):
    events: list[dict[str, Any]] = []


@router.get("")
def list_conversations() -> dict[str, Any]:
    """Every conversation, bucketed by group, for the rail."""
    return {"groups": STORE.grouped(), "defaultGroup": DEFAULT_GROUP}


@router.post("")
def create_conversation(payload: CreatePayload) -> dict[str, Any]:
    conversation = STORE.create(title=payload.title, group=payload.group)
    return conversation.to_dict(has_project=False)


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    conversation = STORE.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {
        **conversation.to_dict(has_project=STORE.has_project(conversation_id)),
        "events": STORE.events(conversation_id),
    }


@router.patch("/{conversation_id}")
def update_conversation(conversation_id: str, payload: UpdatePayload) -> dict[str, Any]:
    """Rename, or move between groups."""
    conversation = STORE.update(conversation_id, title=payload.title, group=payload.group)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict(has_project=STORE.has_project(conversation_id))


@router.put("/{conversation_id}/events")
def put_events(conversation_id: str, payload: EventsPayload) -> dict[str, Any]:
    """Persist the transcript. Called as the chat progresses, not only on close —
    a crashed tab must not cost the user the conversation."""
    conversation = STORE.save_events(conversation_id, payload.events)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict(has_project=STORE.has_project(conversation_id))


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict[str, Any]:
    """Delete the conversation AND its project package.

    Irreversible, and it takes the model with it — the package is the only copy
    once the session has moved on. The UI must confirm before calling this.
    """
    return {"id": conversation_id, "deleted": STORE.delete(conversation_id)}


@router.post("/{conversation_id}/snapshot")
async def snapshot_project(conversation_id: str) -> dict[str, Any]:
    """Capture the live Ragnarok session into this conversation's package.

    Called after the agent changes the model, so the conversation's stored world
    keeps up with the transcript that describes it. The journal ``version`` is
    recorded alongside, which is what lets the UI tell a stale package from a
    current one instead of guessing.
    """
    if STORE.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        meta = await CLIENT.session_meta()
        if not meta:
            raise HTTPException(
                status_code=400,
                detail="Ragnarok has no model loaded, so there is nothing to snapshot.",
            )
        data = await CLIENT.export_project()
    except RagnarokUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RagnarokError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    version = meta.get("version") if isinstance(meta, dict) else None
    conversation = STORE.save_project(conversation_id, data, version=version)
    if conversation is None:  # deleted between the checks
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {
        **conversation.to_dict(has_project=True),
        "bytes": len(data),
    }


@router.post("/{conversation_id}/activate")
async def activate_conversation(conversation_id: str) -> dict[str, Any]:
    """Make this conversation the live one: load its project into Ragnarok.

    With a package, Ragnarok's importer restores the model, its time-series, the
    solved result and the embedded chat — the world the transcript was written
    against. Without one (a conversation that has not touched a model yet) the
    session is CLEARED rather than left alone, so a new chat cannot silently
    inherit the previous conversation's model and have the agent reason about it.
    """
    conversation = STORE.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    data = STORE.read_project(conversation_id)
    try:
        if data is None:
            await CLIENT.clear_session()
            loaded = None
        else:
            bundle = await CLIENT.import_project(data, f"{conversation_id}.zip")
            loaded = {
                "filename": bundle.get("filename"),
                "hasResult": bool(bundle.get("result")),
                "sheets": sorted((bundle.get("model") or {}).keys()),
            }
        meta = await CLIENT.session_meta()
    except RagnarokUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RagnarokError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    return {
        **conversation.to_dict(has_project=data is not None),
        "events": STORE.events(conversation_id),
        "loaded": loaded,
        "sessionMeta": meta,
    }


@router.get("/{conversation_id}/project")
def download_project(conversation_id: str) -> Any:
    """The conversation's project package, for handing to someone else.

    It is Ragnarok's own format, so the recipient opens it in Ragnarok directly —
    no Bifrost required to read a Bifrost export.
    """
    from fastapi.responses import Response

    data = STORE.read_project(conversation_id)
    if data is None:
        raise HTTPException(
            status_code=404, detail="This conversation has no project package yet."
        )
    conversation = STORE.get(conversation_id)
    stem = (conversation.title if conversation else conversation_id) or conversation_id
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in stem).strip() or conversation_id
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )


@router.post("/import")
async def import_conversation(file: UploadFile) -> dict[str, Any]:
    """Adopt a project package as a new conversation, and activate it.

    The inverse of the download above, and the reason a conversation is portable:
    the package carries the chat as well as the model, so importing one recreates
    both rather than dropping a model in with no story attached.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    title = (file.filename or "Imported conversation").rsplit(".", 1)[0]
    conversation = STORE.create(title=title)
    try:
        bundle = await CLIENT.import_project(data, file.filename or "project.zip")
    except RagnarokUnavailable as exc:
        STORE.delete(conversation.id)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RagnarokError as exc:
        STORE.delete(conversation.id)
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    STORE.save_project(conversation.id, data)
    events = (bundle.get("conversation") or {}).get("events") or []
    if isinstance(events, list) and events:
        STORE.save_events(conversation.id, events)
    return {
        **(STORE.get(conversation.id) or conversation).to_dict(has_project=True),
        "events": STORE.events(conversation.id),
    }
