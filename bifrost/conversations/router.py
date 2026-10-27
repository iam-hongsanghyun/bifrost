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
    """Every conversation, bucketed by group, plus which one was live.

    `activeId` is what lets returning to Bifrost resume where the user left off,
    rather than dropping them on an empty shell beside a session that still holds
    their model.
    """
    return {
        "groups": STORE.grouped(),
        "defaultGroup": DEFAULT_GROUP,
        "activeId": STORE.get_active(),
    }


@router.post("")
def create_conversation(payload: CreatePayload) -> dict[str, Any]:
    conversation = STORE.create(title=payload.title, group=payload.group)
    return conversation.to_dict(has_project=False)


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    from .. import assumptions

    conversation = STORE.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {
        **conversation.to_dict(has_project=STORE.has_project(conversation_id)),
        "events": STORE.events(conversation_id),
        "assumptions": assumptions.load(conversation_id),
    }


@router.get("/{conversation_id}/charts/{chart_id}")
def get_chart(conversation_id: str, chart_id: str) -> dict[str, Any]:
    """One chart's ``x`` / ``series`` arrays.

    The transcript lists a chart as a light stub (``lazy: true``) so opening a
    conversation does not ship megabytes of points; the page asks for each chart
    here as it scrolls into view.
    """
    if STORE.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    payload = STORE.chart(conversation_id, chart_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Chart not found.")
    return payload


@router.get("/{conversation_id}/assumptions")
def get_assumptions(conversation_id: str) -> dict[str, Any]:
    """Every number the agent supplied itself, with its basis.

    Separate from the transcript on purpose: "which figures are assumed?" is a
    question about the model, and answering it should not require re-reading a
    conversation.
    """
    from .. import assumptions

    if STORE.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"assumptions": assumptions.load(conversation_id)}


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


def _sidecar(conversation_id: str) -> dict[str, Any] | None:
    """The conversation as the chat sidecar Ragnarok embeds in a project package.

    ``{version, conversationId, title, model, autonomy, events}`` with each chart's
    points inline, so the package stays complete when it is handed to someone else.
    ``None`` while there is no transcript: an empty sidecar would only be noise.
    """
    from ..agent import service

    conversation = STORE.get(conversation_id)
    events = STORE.events_full(conversation_id)
    if conversation is None or not events:
        return None
    return {
        "version": 1,
        "conversationId": conversation_id,
        "title": conversation.title,
        "model": service.peek_model(CLIENT.session_id),
        "autonomy": service.peek_autonomy(CLIENT.session_id),
        "events": events,
    }


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
        data = await CLIENT.export_project(conversation=_sidecar(conversation_id))
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

    previous = STORE.get_active()
    try:
        meta_before = await CLIENT.session_meta()
    except (RagnarokUnavailable, RagnarokError):
        meta_before = {}

    # Never destroy unsaved work. Whatever is in the session belongs to whichever
    # conversation was last live, so capture it into THAT conversation before this
    # one takes the session over. Snapshotting is otherwise manual, and a model the
    # agent built but nobody pressed "Save model" for would be gone the moment the
    # user came back to it.
    if meta_before and previous and previous != conversation_id:
        try:
            outgoing = await CLIENT.export_project(conversation=_sidecar(previous))
            STORE.save_project(previous, outgoing, version=meta_before.get("version"))
            logger.info("Auto-saved the model of conversation %s before switching", previous)
        except (RagnarokUnavailable, RagnarokError):
            logger.exception("Could not auto-save conversation %s; not switching", previous)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not save the current model before switching, so the switch "
                    "was cancelled rather than risk losing it."
                ),
            ) from None

    data = STORE.read_project(conversation_id)
    try:
        if data is None and previous == conversation_id and meta_before:
            # Re-opening the conversation that already owns the session — for
            # example after a reload. Adopt what is loaded instead of clearing it:
            # this is the case where clearing destroyed the model the agent had just
            # built in this very conversation.
            try:
                adopted = await CLIENT.export_project(conversation=_sidecar(conversation_id))
                STORE.save_project(conversation_id, adopted, version=meta_before.get("version"))
                data = adopted
                logger.info("Adopted the live session into conversation %s", conversation_id)
            except (RagnarokUnavailable, RagnarokError):
                logger.exception("Could not adopt the live session into %s", conversation_id)
        if data is None:
            await CLIENT.clear_session()
            loaded = None
        else:
            bundle = await CLIENT.open_project(data, f"{conversation_id}.zip")
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

    from .. import assumptions

    STORE.set_active(conversation_id)
    return {
        **conversation.to_dict(has_project=data is not None),
        "events": STORE.events(conversation_id),
        "assumptions": assumptions.load(conversation_id),
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


def _sidecar_events(data: bytes) -> list[Any]:
    """The chat embedded in a project package, read straight from the zip.

    FALLBACK only. A current Ragnarok returns the sidecar as ``bundle["conversation"]``
    from ``/api/import/project/load``; an older one does not surface the
    ``<stem>.conversation.json`` file at all, so for those the zip is the only way to
    keep the transcript. Absent or unreadable is simply "no chat".
    """
    import io
    import json
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".conversation.json")), None)
            if name is None:
                return []
            events = (json.loads(zf.read(name).decode("utf-8")) or {}).get("events")
    except (zipfile.BadZipFile, ValueError, OSError, AttributeError):
        return []
    return events if isinstance(events, list) else []


def _bundle_events(bundle: dict[str, Any], data: bytes) -> list[Any]:
    """The package's chat: Ragnarok's returned ``conversation`` when it sends the key
    (even ``None``: that is its answer, "no chat"), else the zip, for older Ragnarok."""
    if "conversation" in bundle:
        events = (bundle["conversation"] or {}).get("events")
        return events if isinstance(events, list) else []
    return _sidecar_events(data)


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
    # Parse first, so a package Ragnarok refuses never becomes a conversation.
    try:
        bundle = await CLIENT.import_project(data, file.filename or "project.zip")
    except RagnarokUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RagnarokError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    conversation = STORE.create(title=title)
    STORE.save_project(conversation.id, data)
    events = _bundle_events(bundle, data)
    if isinstance(events, list) and events:
        STORE.save_events(conversation.id, events)
    # Activating is what loads the model into the shared session — and what saves
    # the previous conversation's unsaved model first. Loading it here directly
    # would replace that model without capturing it anywhere.
    try:
        return await activate_conversation(conversation.id)
    except HTTPException:
        STORE.delete(conversation.id)
        raise
