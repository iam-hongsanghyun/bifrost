"""``/api/agent`` — the embedded Bifrost agent endpoints.

All results stream over the session's SSE channel (``GET /api/events``); these
endpoints are the command side: send a message, resolve an approval card,
interrupt, read status/history. Mounted unconditionally — ``/status`` reports a
degraded state when the SDK or credentials are missing, so the Assistant tab
can render setup guidance instead of a dead chat box.
"""
from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi import File as FastAPIFile
from pydantic import BaseModel

from . import AGENT_AVAILABLE, approvals, attachments, auth, policy, questions, service, transcript

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/status")
def agent_status(session_id: str = Query("default", alias="session_id")) -> dict[str, Any]:
    """Availability + auth + live-session state, for the Assistant tab."""
    agent_auth = auth.resolve()
    session = service.get(session_id)
    return {
        "available": AGENT_AVAILABLE,
        "cliFound": bool(shutil.which("claude")) or AGENT_AVAILABLE,
        "auth": agent_auth.kind,
        "active": session is not None,
        "conversationId": session.conversation_id if session else service.peek_conversation_id(session_id),
        "state": session.state if session else "idle",
        "autonomy": session.autonomy if session else service.peek_autonomy(session_id),
        "model": session.model if session else service.peek_model(session_id),
        "models": service.model_choices(),
        "effort": session.effort if session else service.peek_effort(session_id),
        "efforts": list(service.EFFORT_CHOICES),
        "costUsd": round(session.cost_usd, 4) if session else 0.0,
        "pendingApprovals": approvals.pending_for(session.conversation_id) if session else [],
        "pendingQuestions": questions.pending_for(session.conversation_id) if session else [],
    }


class ChatRequest(BaseModel):
    message: str
    sessionId: str = "default"
    conversationId: str | None = None
    autonomy: str | None = None  # manual | guided | auto
    newConversation: bool = False


@router.post("/chat")
async def agent_chat(payload: ChatRequest) -> dict[str, Any]:
    """Send a user message; the reply streams over /api/events."""
    if not AGENT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="The embedded agent needs claude-agent-sdk — install "
                   "backend/app/agent/requirements-agent.txt into the app venv.",
        )
    if auth.resolve().kind is None:
        raise HTTPException(
            status_code=503,
            detail="No Claude credential configured. Run `claude setup-token` and "
                   "save it in Settings → API keys as claude_code_oauth_token "
                   "(or set anthropic_api_key).",
        )
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Empty message.")
    if payload.newConversation:
        session = await service.reset(payload.sessionId)
    else:
        session = service.get_or_create(payload.sessionId)
    if session.busy and payload.conversationId not in (None, session.conversation_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "The agent is still working on another conversation. Wait for it to "
                "finish, or press Stop there first."
            ),
        )
    conversation_id = await session.chat(message, autonomy=payload.autonomy)
    return {"conversationId": conversation_id}


class ApprovalDecisionRequest(BaseModel):
    decision: str  # 'allow' | 'deny'
    editedInput: dict[str, Any] | None = None
    note: str = ""


@router.post("/approvals/{approval_id}")
def agent_approve(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
    """Resolve a pending approval card (Approve / Skip)."""
    decision = approvals.Decision(
        allow=payload.decision == "allow",
        edited_input=payload.editedInput,
        note=payload.note,
    )
    resolved = approvals.resolve(approval_id, decision)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Approval not found (already resolved or timed out).")
    return {"resolved": True, "approvalId": approval_id, "decision": payload.decision}


class QuestionAnswerRequest(BaseModel):
    answer: str


@router.post("/questions/{question_id}")
def agent_answer_question(question_id: str, payload: QuestionAnswerRequest) -> dict[str, Any]:
    """Answer a pending ask_user decision card (an option or free text)."""
    answer = payload.answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="Empty answer.")
    resolved = questions.resolve(question_id, answer)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Question not found (already answered or timed out).")
    return {"resolved": True, "questionId": question_id}


@router.post("/files")
async def agent_upload_files(
    session_id: str = Query("default", alias="session_id"),
    conversation_id: str | None = Query(None, alias="conversation_id"),
    files: list[UploadFile] = FastAPIFile(...),
) -> dict[str, Any]:
    """Store conversation attachments (chat drag-drop / + button).

    Files land in the CONVERSATION's directory so they travel with it; the agent
    reads them with read_attachment. Values: name, storedAs, path, size per file.
    Falls back to the live session's conversation when the caller does not name
    one, so an older client keeps working.
    """
    target = conversation_id or service.peek_conversation_id(session_id) or session_id
    if len(files) > attachments.MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files (max {attachments.MAX_FILES_PER_UPLOAD} per drop).",
        )
    saved = []
    for f in files:
        try:
            saved.append(attachments.save(target, f.filename or "file", f.file))
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"files": saved}


class ModelRequest(BaseModel):
    sessionId: str = "default"
    model: str | None = None  # None/empty → SDK default


@router.post("/model")
async def agent_set_model(payload: ModelRequest) -> dict[str, Any]:
    """Select the model the agent runs on (persists across restarts)."""
    session = service.get_or_create(payload.sessionId)
    try:
        await session.set_model(payload.model)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The agent is mid-turn, and this takes effect at the start of one. "
                "Wait for it to finish, or press Stop, then try again."
            ),
        ) from exc
    return {"model": session.model, "models": service.model_choices()}


class EffortRequest(BaseModel):
    sessionId: str = "default"
    effort: str = "high"


@router.post("/effort")
async def agent_set_effort(payload: EffortRequest) -> dict[str, Any]:
    """Select reasoning effort (persists across restarts)."""
    session = service.get_or_create(payload.sessionId)
    try:
        await session.set_effort(payload.effort)
    except ValueError as exc:
        # A bad level is the caller's mistake, not a busy agent — say which.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The agent is mid-turn, and this takes effect at the start of one. "
                "Wait for it to finish, or press Stop, then try again."
            ),
        ) from exc
    return {"effort": session.effort, "efforts": list(service.EFFORT_CHOICES)}


class ConversationImportRequest(BaseModel):
    sessionId: str = "default"
    model: str | None = None
    autonomy: str | None = None
    events: list[dict[str, Any]] = []


@router.get("/conversation/export")
def agent_conversation_export(
    session_id: str = Query("default", alias="session_id"),
) -> dict[str, Any]:
    """The current conversation as a portable payload (for project export)."""
    return {"conversation": service.export_conversation(session_id)}


@router.post("/conversation/import")
async def agent_conversation_import(payload: ConversationImportRequest) -> dict[str, Any]:
    """Restore an exported conversation (events + model + autonomy)."""
    if not payload.events:
        raise HTTPException(status_code=422, detail="No conversation events to import.")
    conversation_id = await service.import_conversation(
        payload.sessionId, payload.events, model=payload.model, autonomy=payload.autonomy,
    )
    session = service.get(payload.sessionId)
    return {
        "conversationId": conversation_id,
        "model": session.model if session else payload.model,
        "autonomy": session.autonomy if session else payload.autonomy,
    }


@router.post("/conversation/import-file")
async def agent_conversation_import_file(
    session_id: str = Query("default", alias="session_id"),
    file: UploadFile = FastAPIFile(...),
) -> dict[str, Any]:
    """Import a conversation from a file — a Ragnarok Project ``.zip`` (its
    ``*.conversation.json`` member) or a bare conversation ``.json``."""
    import json as _json
    import zipfile
    from io import BytesIO

    raw = await file.read()
    name = (file.filename or "").lower()
    payload: dict[str, Any] | None = None
    try:
        if name.endswith(".zip") or raw[:2] == b"PK":
            with zipfile.ZipFile(BytesIO(raw)) as zf:
                member = next(
                    (n for n in zf.namelist() if n.lower().endswith(".conversation.json")),
                    None,
                )
                if member is None:
                    raise HTTPException(
                        status_code=400,
                        detail="This project file carries no saved conversation.",
                    )
                payload = _json.loads(zf.read(member).decode("utf-8"))
        else:
            data = _json.loads(raw.decode("utf-8"))
            # Accept a bare conversation payload or a bundle wrapping one.
            payload = data.get("conversation") if isinstance(data.get("conversation"), dict) else data
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Conversation import failed: {exc}") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="No conversation events found in the file.")
    conversation_id = await service.import_conversation(
        session_id,
        events,
        model=payload.get("model"),
        autonomy=payload.get("autonomy"),
    )
    session = service.get(session_id)
    return {
        "conversationId": conversation_id,
        "model": session.model if session else None,
        "autonomy": session.autonomy if session else None,
    }


class ClearConversationRequest(BaseModel):
    sessionId: str = "default"


@router.post("/conversation/clear")
async def agent_conversation_clear(payload: ClearConversationRequest) -> dict[str, Any]:
    """Clear the conversation: reset the live session and delete its transcript."""
    conversation_id = await service.clear_conversation(payload.sessionId)
    return {"conversationId": conversation_id}


class InterruptRequest(BaseModel):
    sessionId: str = "default"


@router.post("/interrupt")
async def agent_interrupt(payload: InterruptRequest) -> dict[str, Any]:
    """Stop the current turn (pending approvals are denied as cancelled)."""
    session = service.get(payload.sessionId)
    if session is None:
        return {"interrupted": False}
    await session.interrupt()
    return {"interrupted": True}


@router.get("/history")
def agent_history(
    session_id: str = Query("default", alias="session_id"),
    conversation_id: str | None = Query(None, alias="conversationId"),
) -> dict[str, Any]:
    """Persisted transcript events (re-render the chat after a page reload)."""
    if conversation_id is None:
        # Persisted state wins over "newest file" so a cleared conversation
        # stays cleared and a restored one stays selected across reloads.
        conversation_id = service.peek_conversation_id(session_id)
        if conversation_id is None:
            return {"conversationId": None, "events": []}
    return {
        "conversationId": conversation_id,
        "events": transcript.read(session_id, conversation_id),
    }


@router.get("/policy")
def agent_policy() -> dict[str, Any]:
    """The gating tiers (for the autonomy selector's explanatory UI)."""
    return {
        "levels": list(policy.AUTONOMY_LEVELS),
        "readOnly": sorted(policy.READ_ONLY),
        "cheap": sorted(policy.CHEAP_EDITS),
        "alwaysGated": sorted(policy.ALWAYS_GATED),
        "expensive": sorted(policy.EXPENSIVE),
    }
