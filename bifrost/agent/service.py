"""The embedded agent session host — Claude Agent SDK wired to Ragnarok.

One :class:`AgentSession` per model session. The SDK client runs in streaming
-input mode on the app's event loop; a consumer task translates SDK messages
into the session's SSE channel (:mod:`backend.app.events_bus`), so the chat
pane, tool cards, and approval cards are all driven by the same one-way stream
the journal already uses.

Tool access: the SDK spawns the existing ``backend/mcp`` server as a stdio
subprocess pointed back at THIS backend with ``RAGNAROK_SESSION_ID=<session>``
and ``RAGNAROK_MCP_AUTONOMY=auto`` (no model-facing confirm dance) — gating is
enforced here via ``can_use_tool`` → GUI approval cards (:mod:`.policy`,
:mod:`.approvals`). Every mutation the agent makes therefore flows through the
REST API, lands in the mutation journal tagged ``actor=agent`` with this
conversation's id, and repaints the GUI live.

The SDK is an optional dependency: this module imports it lazily so the app
(and the Mjolnir subtree) works without it. Tests inject a fake backend via
``AgentSession(backend_factory=...)`` — the seam is :class:`AgentBackend`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

# Ragnarok journals every mutation at its own store facade, whoever made it,
# so Bifrost keeps no second journal — it reads Ragnarok's over HTTP when it
# needs the timeline. Only the local event bus (chat streaming) is ours.
from .. import events_bus, ragnarok
from . import approvals, auth, policy, prompts, questions, transcript

logger = logging.getLogger("pypsa_gui.agent")

_REPO_ROOT = Path(__file__).resolve().parents[3]

MAX_TURNS = int(os.environ.get("RAGNAROK_AGENT_MAX_TURNS", "50"))
MAX_BUDGET_USD = float(os.environ.get("RAGNAROK_AGENT_MAX_BUDGET_USD", "0") or 0) or None
API_BASE = os.environ.get("RAGNAROK_AGENT_API_BASE", "http://127.0.0.1:8000")
DEFAULT_MODEL = os.environ.get("RAGNAROK_AGENT_MODEL") or None
# Selectable models for the chat's picker; env-overridable (comma-separated ids).
_MODEL_CHOICE_DEFAULTS = "claude-opus-4-8,claude-sonnet-5,claude-haiku-4-5"
# Cap on the rendered prior-transcript context a fresh SDK session is primed with.
RESTORED_CONTEXT_MAX_CHARS = int(os.environ.get("RAGNAROK_AGENT_RESTORE_MAX_CHARS", "20000"))


def model_choices() -> list[str]:
    """Model ids offered in the model picker (RAGNAROK_AGENT_MODELS overrides)."""
    raw = os.environ.get("RAGNAROK_AGENT_MODELS", _MODEL_CHOICE_DEFAULTS)
    out: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    if DEFAULT_MODEL and DEFAULT_MODEL not in out:
        out.insert(0, DEFAULT_MODEL)
    return out


class AgentBackend(Protocol):
    """The slice of ClaudeSDKClient the service uses (test seam)."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_messages(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


def _summarize_args(args: dict[str, Any], limit: int = 240) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(args)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summarize_result(content: Any, limit: int = 400) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
            if text:
                parts.append(str(text))
        content = "\n".join(parts) or str(content)
    text = str(content or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_restored_context(events: list[dict[str, Any]]) -> str:
    """Render a persisted transcript into a context block for a fresh SDK session.

    A restored conversation (project import / backend restart) has its history
    on disk but a brand-new model session. Priming the first query with this
    block is what lets the agent literally continue where the user left off.
    """
    lines: list[str] = []
    question_text: dict[str, str] = {}
    approval_tool: dict[str, str] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = e.get("event")
        if kind == "chat.message":
            role = "User" if e.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {e.get('markdown', '')}")
        elif kind == "tool.start":
            lines.append(f"[tool {e.get('tool')} args={e.get('argsSummary', '')}]")
        elif kind == "tool.end":
            ok = "ok" if e.get("ok") else "FAILED"
            lines.append(f"[tool result {ok}: {e.get('resultSummary', '')}]")
        elif kind == "question.request":
            question_text[str(e.get("questionId"))] = str(e.get("question", ""))
        elif kind == "question.resolved":
            q = question_text.get(str(e.get("questionId")), "")
            lines.append(f"[you asked: {q} — user answered: {e.get('answer', '')}]")
        elif kind == "approval.request":
            approval_tool[str(e.get("approvalId"))] = str(e.get("tool", ""))
        elif kind == "approval.resolved":
            tool = approval_tool.get(str(e.get("approvalId")), "")
            lines.append(f"[approval for {tool}: {e.get('decision', '')}]")
    body = "\n".join(lines)
    if len(body) > RESTORED_CONTEXT_MAX_CHARS:
        body = "(earlier turns omitted)\n…" + body[-RESTORED_CONTEXT_MAX_CHARS:]
    return (
        "<restored_conversation>\n"
        "This conversation was restored from a saved transcript (project import or "
        "app restart) — your live session is new, but the log below is your own "
        "prior history with this user. Continue seamlessly from it. The model/world "
        "state may have changed since; verify with tools before relying on earlier "
        "readings.\n---\n"
        f"{body}\n"
        "</restored_conversation>"
    )


class AgentSession:
    """One conversation-capable agent bound to a model session."""

    def __init__(
        self,
        session_id: str,
        *,
        backend_factory: Callable[["AgentSession"], AgentBackend] | None = None,
    ) -> None:
        self.session_id = session_id
        self.conversation_id = uuid.uuid4().hex[:12]
        self.autonomy: str = "guided"
        self.model: str | None = DEFAULT_MODEL
        self.state: str = "idle"
        self.cost_usd: float = 0.0
        self.turns: int = 0
        self._backend: AgentBackend | None = None
        self._consumer: asyncio.Task | None = None
        self._backend_factory = backend_factory or _sdk_backend
        # callId → journal version when the tool started (diff correlation).
        self._tool_versions: dict[str, int] = {}
        self._stream_message_id: str | None = None
        # Set by interrupt(); makes the post-abort ResultMessage report cleanly.
        self._interrupted = False

    # ── persisted session state ─────────────────────────────────────────────

    def _persist_state(self) -> None:
        """Write {conversationId, model, autonomy} so a restart resumes here."""
        transcript.write_state(self.session_id, {
            "conversationId": self.conversation_id,
            "model": self.model,
            "autonomy": self.autonomy,
        })

    # ── event plumbing ──────────────────────────────────────────────────────

    def _publish(self, event: str, data: dict[str, Any]) -> None:
        events_bus.publish(self.session_id, event, data)
        if event in (
            "chat.message", "tool.start", "tool.end", "agent.status",
            "approval.request", "approval.resolved",
            "question.request", "question.resolved",
        ):
            transcript.append(self.session_id, self.conversation_id, event, data)

    async def _ragnarok_version(self) -> int | None:
        """Ragnarok's journal version right now, or ``None`` if it cannot be read.

        Used to bracket a tool call so `tool.end` can report which mutations that
        tool produced. Never raises: a journal hiccup must not break the chat
        stream, and losing the correlation only costs a UI affordance.
        """
        try:
            meta = await ragnarok.CLIENT.session_meta()
        except Exception:  # noqa: BLE001 — best-effort attribution
            return None
        version = (meta or {}).get("version")
        return int(version) if isinstance(version, int) else None

    async def _ragnarok_entries_since(self, since: int | None) -> list[int]:
        """Journal entry ids newer than ``since``.

        Filtered by VERSION WINDOW only. In-process this used to also filter on
        `origin.conversationId`, which Ragnarok cannot know over HTTP — its actor
        middleware records *that* an agent made the change, not which conversation
        it came from. So with two conversations mutating one session concurrently
        these ids could over-report. That is not reachable today (a session hosts
        one active conversation at a time, see `/activate`), and the honest fix is
        for Bifrost to send a conversation header that Ragnarok's journal records
        — a small change there, not a guess here.
        """
        if since is None:
            return []
        try:
            payload = await ragnarok.CLIENT.journal(limit=30)
        except Exception:  # noqa: BLE001
            return []
        entries = (payload or {}).get("entries") or []
        return sorted(
            int(e["id"]) for e in entries
            if isinstance(e, dict) and isinstance(e.get("id"), int) and e["id"] > since
        )

    def _set_state(self, state: str, **extra: Any) -> None:
        self.state = state
        self._publish("agent.status", {
            "conversationId": self.conversation_id,
            "state": state,
            "costUsd": round(self.cost_usd, 4),
            "turns": self.turns,
            "autonomy": self.autonomy,
            **extra,
        })

    # ── permission gate (SDK can_use_tool) ──────────────────────────────────

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], _ctx: Any):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if tool_name.startswith(policy.BIFROST_PREFIX):
            return PermissionResultAllow()  # interaction tools (ask_user)
        if not tool_name.startswith(policy.MCP_PREFIX):
            return PermissionResultDeny(
                message="Only the ragnarok modelling tools are enabled in Bifrost.",
                interrupt=False,
            )
        if not policy.requires_approval(tool_name, self.autonomy):
            return PermissionResultAllow()
        approval = approvals.create(self.conversation_id, policy.short_tool_name(tool_name), tool_input)
        self._publish("approval.request", approval.card())
        self._set_state("awaiting_approval")
        decision = await approvals.wait(approval)
        self._publish("approval.resolved", {
            "approvalId": approval.id,
            "conversationId": self.conversation_id,
            "decision": "allow" if decision.allow else "deny",
            "by": decision.by,
        })
        self._set_state("thinking")
        if decision.allow:
            return PermissionResultAllow(updated_input=decision.edited_input or tool_input)
        return PermissionResultDeny(
            message=decision.note or "The user skipped this step — adapt the plan or ask.",
            # A user Stop must ABORT the turn; an ordinary Skip feeds back so
            # the agent re-plans around the denied step.
            interrupt=decision.by == "stop",
        )

    # ── ask_user (in-process bifrost tool) ──────────────────────────────────

    async def _handle_ask_user(self, args: dict[str, Any]) -> str:
        """Hand a decision to the user: publish an option card, await the answer.

        Returns the user's answer text (an option or free text), or the
        no-answer sentinel on timeout/interrupt so the agent proceeds and says
        which assumption it made.
        """
        question_text = str(args.get("question") or "").strip()
        options = args.get("options") if isinstance(args.get("options"), list) else []
        if not question_text:
            return "(ask_user needs a 'question')"
        q = questions.create(self.conversation_id, question_text, options)
        self._publish("question.request", q.card())
        self._set_state("awaiting_input")
        answer = await questions.wait(q)
        self._publish("question.resolved", {
            "questionId": q.id,
            "conversationId": self.conversation_id,
            "answer": answer,
        })
        self._set_state("thinking")
        return answer

    # ── SDK message consumer ────────────────────────────────────────────────

    async def _consume(self) -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        assert self._backend is not None
        try:
            async for message in self._backend.receive_messages():
                if isinstance(message, StreamEvent):
                    event = getattr(message, "event", None) or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if self._stream_message_id is None:
                                self._stream_message_id = uuid.uuid4().hex[:12]
                            self._publish("chat.delta", {
                                "conversationId": self.conversation_id,
                                "messageId": self._stream_message_id,
                                "text": delta["text"],
                            })
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            self._publish("chat.message", {
                                "conversationId": self.conversation_id,
                                "messageId": self._stream_message_id or uuid.uuid4().hex[:12],
                                "role": "assistant",
                                "markdown": block.text,
                            })
                            self._stream_message_id = None
                        elif isinstance(block, ToolUseBlock):
                            self._tool_versions[block.id] = await self._ragnarok_version()
                            self._set_state("running_tool")
                            self._publish("tool.start", {
                                "conversationId": self.conversation_id,
                                "callId": block.id,
                                "tool": policy.short_tool_name(block.name),
                                "argsSummary": _summarize_args(block.input or {}),
                                "gated": policy.requires_approval(block.name, self.autonomy),
                            })
                elif isinstance(message, UserMessage):
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            since = self._tool_versions.pop(block.tool_use_id, None)
                            entry_ids = await self._ragnarok_entries_since(since)
                            self._publish("tool.end", {
                                "conversationId": self.conversation_id,
                                "callId": block.tool_use_id,
                                "ok": not bool(block.is_error),
                                "resultSummary": _summarize_result(block.content),
                                "journalEntryIds": sorted(entry_ids),
                            })
                    if not self._tool_versions:
                        self._set_state("thinking")
                elif isinstance(message, ResultMessage):
                    self.turns = message.num_turns or self.turns
                    if message.total_cost_usd:
                        self.cost_usd = message.total_cost_usd
                    self._stream_message_id = None
                    if self._interrupted:
                        # The abort's ResultMessage often reads as an error —
                        # report it as the stop the user asked for instead.
                        self._interrupted = False
                        self._set_state("done", stopReason="interrupted")
                    else:
                        self._set_state(
                            "error" if message.is_error else "done",
                            stopReason=str(message.stop_reason or ""),
                        )
                    self.state = "idle"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the app
            logger.exception("agent consumer failed (%s)", self.session_id)
            self._set_state("error", error=str(exc)[:300])
            self.state = "idle"

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return self.state in ("thinking", "running_tool", "awaiting_approval")

    async def chat(self, message: str, *, autonomy: str | None = None) -> str:
        if autonomy:
            self.autonomy = policy.normalize_autonomy(autonomy)
        prompt = message
        if self._backend is None:
            # Fresh SDK session on a conversation with prior history (restored
            # project / restart / model switch): prime the first query with the
            # transcript so the model continues where the user left off.
            prior = transcript.read(self.session_id, self.conversation_id)
            if prior:
                prompt = _render_restored_context(prior) + "\n\n" + message
            self._backend = self._backend_factory(self)
            await self._backend.connect()
            self._consumer = asyncio.create_task(self._consume())
        transcript.append(self.session_id, self.conversation_id, "chat.message", {
            "conversationId": self.conversation_id,
            "messageId": uuid.uuid4().hex[:12],
            "role": "user",
            "markdown": message,
        })
        self._persist_state()
        self._interrupted = False
        self._set_state("thinking")
        await self._backend.query(prompt)
        return self.conversation_id

    async def set_model(self, model: str | None) -> None:
        """Select the model for this session's next turns.

        Options are baked into the SDK client at connect time, so a live
        backend is disposed; the next chat() reconnects with the new model and
        the transcript replay keeps the conversation's context.
        """
        model = (model or "").strip() or None
        if model == self.model:
            return
        if self.busy:
            raise RuntimeError("agent_busy")
        self.model = model
        await self._dispose_backend()
        self._persist_state()

    async def interrupt(self) -> None:
        """Stop the current turn NOW.

        Order matters: tell the SDK to abort the turn FIRST, then unwind any
        gate the model is blocked on. A pending approval resolves as a
        ``by="stop"`` deny (→ PermissionResultDeny(interrupt=True), aborting
        rather than re-planning); a pending question resolves with the
        no-answer sentinel. The reverse order let the turn continue: the deny
        fed back before the abort reached the CLI.
        """
        self._interrupted = True
        if self._backend is not None:
            try:
                await self._backend.interrupt()
            except Exception:  # noqa: BLE001 — interrupt is best-effort
                logger.warning("agent interrupt failed (%s)", self.session_id, exc_info=True)
        approvals.cancel_for(self.conversation_id)
        questions.cancel_for(self.conversation_id)
        self._set_state("idle", stopReason="interrupted")

    async def _dispose_backend(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        if self._backend is not None:
            try:
                await self._backend.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._backend = None

    async def close(self) -> None:
        approvals.cancel_for(self.conversation_id)
        questions.cancel_for(self.conversation_id)
        await self._dispose_backend()
        self.state = "idle"


# ── real SDK backend ────────────────────────────────────────────────────────────


def _sdk_backend(session: AgentSession) -> AgentBackend:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool

    # In-process "bifrost" server: interaction tools that hand decisions to the
    # user. Runs inside this backend (no subprocess), so the handler can await
    # the GUI's answer directly.
    @tool(
        "ask_user",
        "Ask the USER to decide between concrete options at a major step "
        "(run mode, snapshot window, scenario, carbon price, which dataset). "
        "Returns their answer (an option or free text). Use BEFORE expensive "
        "or study-shaping actions; never invent a preference the user could "
        "state in one click.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "One clear question ending in '?'"},
                "options": {
                    "type": "array", "items": {"type": "string"},
                    "description": "2-6 short, mutually exclusive choices (the user can also type their own).",
                },
            },
            "required": ["question"],
        },
    )
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        answer = await session._handle_ask_user(args)
        return {"content": [{"type": "text", "text": answer}]}

    @tool(
        "read_attachment",
        "Read a text window of a file the user attached to this conversation "
        "(they drop files into the chat; each attachment message lists stored "
        "paths). Use it to inspect CSV/JSON/text data before folding it into "
        "the model with edit_sheet. Binary files report type+size only.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Stored path (or name) from the attachment message."},
                "offset": {"type": "integer", "description": "First line to return (default 0)."},
                "limit": {"type": "integer", "description": "Max lines to return (default 200, cap 2000)."},
            },
            "required": ["path"],
        },
    )
    async def read_attachment(args: dict[str, Any]) -> dict[str, Any]:
        from . import attachments

        result = attachments.read_preview(
            session.session_id,
            str(args.get("path") or ""),
            offset=int(args.get("offset") or 0),
            limit=int(args.get("limit") or 200),
        )
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    bifrost_server = create_sdk_mcp_server(
        name="bifrost", version="1.0.0", tools=[ask_user, read_attachment]
    )

    agent_auth = auth.resolve()
    mcp_env = {
        "PYTHONPATH": str(_REPO_ROOT),
        "RAGNAROK_API_BASE": API_BASE,
        "RAGNAROK_SESSION_ID": session.session_id,
        "RAGNAROK_MCP_AUTONOMY": "auto",  # gating happens in can_use_tool
        "RAGNAROK_MCP_ACTOR": "agent",
        "RAGNAROK_MCP_CONVERSATION": session.conversation_id,
    }
    workdir = _REPO_ROOT / "backend" / "data" / "agent" / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(
        system_prompt=prompts.SYSTEM_PROMPT,
        mcp_servers={
            "ragnarok": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "backend.mcp"],
                "env": mcp_env,
            },
            "bifrost": bifrost_server,
        },
        # Builtin tools are denied in can_use_tool too; this keeps the model
        # from even trying the heavy ones.
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task"],
        can_use_tool=session._can_use_tool,
        include_partial_messages=True,
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        cwd=str(workdir),
        setting_sources=None,  # never load user/project CLAUDE.md or skills
        env=agent_auth.env,
        model=session.model or None,
    )
    return ClaudeSDKClient(options=options)


# ── registry ────────────────────────────────────────────────────────────────────

_sessions: dict[str, AgentSession] = {}


def _restore_prefs(session: AgentSession, state: dict[str, Any]) -> None:
    model = state.get("model")
    if isinstance(model, str) and model.strip():
        session.model = model.strip()
    autonomy = state.get("autonomy")
    if isinstance(autonomy, str) and autonomy:
        session.autonomy = policy.normalize_autonomy(autonomy)


def get(session_id: str) -> AgentSession | None:
    return _sessions.get(session_id)


def get_or_create(session_id: str) -> AgentSession:
    session = _sessions.get(session_id)
    if session is None:
        session = _sessions[session_id] = AgentSession(session_id)
        # Resume where the user left off across restarts: same conversation id
        # (its transcript replays into the fresh SDK session) + model + autonomy.
        state = transcript.read_state(session_id)
        conversation_id = state.get("conversationId")
        if isinstance(conversation_id, str) and conversation_id:
            session.conversation_id = conversation_id
        _restore_prefs(session, state)
    return session


async def reset(session_id: str) -> AgentSession:
    """Dispose the current conversation (keep its transcript) and start fresh.

    Model and autonomy preferences carry over; the persisted state is
    overwritten so a restart doesn't resurrect the old conversation.
    """
    old = _sessions.pop(session_id, None)
    if old is not None:
        await old.close()
    session = _sessions[session_id] = AgentSession(session_id)
    if old is not None:
        session.model = old.model
        session.autonomy = old.autonomy
    else:
        _restore_prefs(session, transcript.read_state(session_id))
    session._persist_state()
    return session


def peek_conversation_id(session_id: str) -> str | None:
    """Current conversation id without creating a session (live → persisted → newest)."""
    session = _sessions.get(session_id)
    if session is not None:
        return session.conversation_id
    state = transcript.read_state(session_id)
    conversation_id = state.get("conversationId")
    if isinstance(conversation_id, str) and conversation_id:
        return conversation_id
    conversations = transcript.list_conversations(session_id)
    return conversations[0]["conversationId"] if conversations else None


def peek_model(session_id: str) -> str | None:
    """Selected model without creating a session (live → persisted → default)."""
    session = _sessions.get(session_id)
    if session is not None:
        return session.model
    model = transcript.read_state(session_id).get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_MODEL


def peek_autonomy(session_id: str) -> str:
    """Autonomy without creating a session (live → persisted → 'guided')."""
    session = _sessions.get(session_id)
    if session is not None:
        return session.autonomy
    return policy.normalize_autonomy(transcript.read_state(session_id).get("autonomy"))


# ── conversation portability (project save / import / clear) ───────────────────


def export_conversation(session_id: str) -> dict[str, Any] | None:
    """The current conversation as a portable payload (None when empty).

    This is what a Ragnarok Project package embeds so that importing the
    project restores the chat — events for the UI, model + autonomy so the
    session picks up exactly where it left off.
    """
    session = _sessions.get(session_id)
    if session is not None:
        conversation_id: str | None = session.conversation_id
        model, autonomy = session.model, session.autonomy
    else:
        state = transcript.read_state(session_id)
        conversation_id = peek_conversation_id(session_id)
        model = state.get("model") if isinstance(state.get("model"), str) else None
        autonomy = policy.normalize_autonomy(state.get("autonomy"))
    if not conversation_id:
        return None
    events = transcript.read(session_id, conversation_id)
    if not events:
        return None
    return {
        "version": 1,
        "conversationId": conversation_id,
        "model": model,
        "autonomy": autonomy,
        "events": events,
    }


async def import_conversation(
    session_id: str,
    events: list[dict[str, Any]],
    *,
    model: str | None = None,
    autonomy: str | None = None,
) -> str:
    """Restore an exported conversation into this session.

    Writes the events as a NEW conversation (fresh id, so the original file is
    never clobbered), points the session at it, and applies the saved model +
    autonomy. The next chat() replays the transcript into the fresh SDK
    session, so the agent continues where the saved conversation left off.
    """
    session = await reset(session_id)
    new_id = session.conversation_id
    rewritten: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event = dict(event)
        if "conversationId" in event:
            event["conversationId"] = new_id
        rewritten.append(event)
    transcript.write_events(session_id, new_id, rewritten)
    session.model = (model or "").strip() or None
    if autonomy:
        session.autonomy = policy.normalize_autonomy(autonomy)
    session._persist_state()
    return new_id


async def clear_conversation(session_id: str) -> str:
    """Clear the conversation: reset the session AND delete its transcript."""
    old_conversation = peek_conversation_id(session_id)
    session = await reset(session_id)
    if old_conversation:
        transcript.delete(session_id, old_conversation)
    return session.conversation_id
