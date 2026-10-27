"""The embedded agent session host — Claude Agent SDK wired to Ragnarok.

One :class:`AgentSession` per model session. The SDK client runs in streaming
-input mode on the app's event loop; a consumer task translates SDK messages
into the session's SSE channel (:mod:`bifrost.events_bus`), so the chat
pane, tool cards, and approval cards are all driven by the same one-way stream
the journal already uses.

Tool access: the SDK spawns Ragnarok's MCP server (``python -m backend.mcp`` from
the chosen checkout, on that checkout's own interpreter) as a stdio subprocess
pointed at the ENGINE's API with ``RAGNAROK_SESSION_ID=<session>``
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
from .. import assumptions, events_bus, ragnarok
from . import approvals, auth, policy, prompts, questions, transcript

logger = logging.getLogger("bifrost.agent")

from .. import engine
from ..charts import build_chart_payload
from ..config import CONFIG

_REPO_ROOT = CONFIG.repo_root

MAX_TURNS = int(os.environ.get("RAGNAROK_AGENT_MAX_TURNS", "50"))
MAX_BUDGET_USD = float(os.environ.get("RAGNAROK_AGENT_MAX_BUDGET_USD", "0") or 0) or None
# Where the MCP subprocess finds the engine. Defaults to the SAME base Bifrost's own
# client uses: this used to default to :8000 while the client defaulted to :8772, so
# the agent's tools edited whatever answered on 8000 — or nothing — while the UI
# watched the engine Bifrost had started.
API_BASE = os.environ.get("RAGNAROK_AGENT_API_BASE", "").strip().rstrip("/") or CONFIG.ragnarok_api_base
DEFAULT_MODEL = os.environ.get("RAGNAROK_AGENT_MODEL") or None
# Selectable models for the chat's picker; env-overridable (comma-separated ids).
_MODEL_CHOICE_DEFAULTS = "claude-opus-5-5,claude-fable-5-1,claude-sonnet-5-5,claude-haiku-5-5"
# Cap on the rendered prior-transcript context a fresh SDK session is primed with.
RESTORED_CONTEXT_MAX_CHARS = int(os.environ.get("RAGNAROK_AGENT_RESTORE_MAX_CHARS", "20000"))


#: Reasoning effort levels the picker offers, cheapest first. The SDK takes these
#: verbatim; "high" is the default because a wrong power-system answer costs far
#: more than the tokens saved by guessing quickly.
EFFORT_CHOICES = ("low", "medium", "high")
DEFAULT_EFFORT = os.environ.get("RAGNAROK_AGENT_EFFORT", "high").strip().lower()
if DEFAULT_EFFORT not in EFFORT_CHOICES:
    DEFAULT_EFFORT = "high"


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
        #: Reasoning effort for the next turns. A modelling question is often
        #: cheap to answer and expensive to get wrong, so this is the user's
        #: dial rather than ours.
        self.effort: str = DEFAULT_EFFORT
        self.state: str = "idle"
        #: The agent's file sandbox: its cwd, and where MCP exports land. Exports
        #: to anywhere else need approval (policy.export_leaves_workdir).
        self.workdir: Path = CONFIG.agent_dir / "workdir"
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
            "effort": self.effort,
            "autonomy": self.autonomy,
        })

    # ── event plumbing ──────────────────────────────────────────────────────

    def _publish(self, event: str, data: dict[str, Any]) -> None:
        events_bus.publish(self.session_id, event, data)
        if event in (
            "chat.message", "tool.start", "tool.end", "agent.status",
            "approval.request", "approval.resolved",
            "question.request", "question.resolved",
            "chart.show", "assumption.recorded", "show.request",
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

        Filtered by version window, and by `origin.conversationId` when the entry
        carries one: the MCP subprocess sends this conversation's id as
        `X-Ragnarok-Conversation` (``RAGNAROK_MCP_CONVERSATION``) and Ragnarok's
        journal records it. An entry with no conversation (a human edit in the
        Ragnarok tab) is kept, as before — the window is all that can be said.
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
            and ((e.get("origin") or {}).get("conversationId") in (None, self.conversation_id))
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
        if not policy.requires_approval(tool_name, self.autonomy, tool_input, self.workdir):
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

    def _handle_show_chart(self, args: dict[str, Any]) -> str:
        """Publish a chart for the transcript to draw.

        Shaping (alignment of gaps, the point budget, thinning that keeps peaks)
        lives in :mod:`bifrost.charts`; this only publishes the result and tells
        the agent what happened, including when its data was thinned.
        """
        payload = build_chart_payload(
            args,
            conversation_id=self.conversation_id,
            max_points=CONFIG.chart_max_points,
            max_cells=CONFIG.chart_max_cells,
        )
        if isinstance(payload, str):
            return payload
        self._publish("chart.show", payload)
        drawn = f"Drawn: {payload['title']} ({len(payload['series'])} series, {len(payload['x'])} points)."
        if payload.get("thinnedFrom"):
            drawn += (
                f" Thinned from {payload['thinnedFrom']} points keeping each stretch's peak "
                "and trough; the user can zoom, but cannot see the thinned-out points."
            )
        return drawn

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
        q = questions.create(
            self.conversation_id, question_text, options,
            needs_file=bool(args.get("needsFile")),
        )
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
                                # SDK plumbing (ToolSearch…) never reaches can_use_tool, so
                                # it is not "gated" however the policy would class it.
                                "gated": block.name.startswith(policy.MCP_PREFIX)
                                and policy.requires_approval(
                                    block.name, self.autonomy, block.input or {}, self.workdir
                                ),
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

    async def set_effort(self, effort: str) -> None:
        """Select reasoning effort for this session's next turns.

        Same mechanics as :meth:`set_model` — baked in at connect time, so the
        live backend is disposed and the next turn reconnects with the transcript
        replayed. Rejects unknown levels rather than passing them to the SDK,
        where a typo would surface as an opaque connect failure mid-turn.
        """
        level = (effort or "").strip().lower()
        if level not in EFFORT_CHOICES:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_CHOICES)}")
        if level == self.effort:
            return
        if self.busy:
            raise RuntimeError("agent_busy")
        self.effort = level
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
        "(run mode, snapshot window, scenario, carbon price, which dataset), or "
        "to HAND YOU DATA with needsFile — a wind or solar profile, a load trace, "
        "a cost table. Returns their answer (an option, free text, or the stored "
        "paths of files they uploaded, which you then read with read_attachment). "
        "Use BEFORE expensive or study-shaping actions; never invent a preference "
        "the user could state in one click, and never synthesise a time series "
        "without offering to take theirs first.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "One clear question ending in '?'"},
                "options": {
                    "type": "array", "items": {"type": "string"},
                    "description": "2-6 short, mutually exclusive choices (the user can also type their own).",
                },
                "needsFile": {
                    "type": "boolean",
                    "description": (
                        "True when the answer is a FILE (CSV/Excel/JSON) rather than a "
                        "choice — the card then shows an upload control. Still give "
                        "`options` for the way out, e.g. 'Synthesise one instead'."
                    ),
                },
            },
            "required": ["question"],
        },
    )
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        answer = await session._handle_ask_user(args)
        return {"content": [{"type": "text", "text": answer}]}

    @tool(
        "record_assumption",
        "Record a number you supplied yourself rather than took from data or from the "
        "user. REQUIRED for any such value before you use it in the model — a "
        "discount rate, a fuel price, an efficiency, a capacity factor, a cost. State "
        "the basis honestly: 'assumed' when it is your own choice, 'user' when they "
        "gave it, 'dataset' when it came from data, 'default' when it is the schema's. "
        "This is what lets the modeller trace every figure back, so recording an "
        "assumption is never optional and never something to batch until the end.",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "What it applies to, e.g. 'discount rate', 'gas marginal cost'."},
                "value": {"type": "string", "description": "The value as text — a range or 'n/a' is fine."},
                "unit": {"type": "string", "description": "%/yr, $/MWh, MW, dimensionless …"},
                "basis": {"type": "string", "enum": ["assumed", "user", "dataset", "default"]},
                "source": {
                    "type": "string",
                    "description": "Where it came from, or your reasoning if you chose it. Be specific: "
                                   "'typical CCGT SRMC for Korea 2024' beats 'reasonable'.",
                },
                "affects": {"type": "string", "description": "What in the model or the result depends on it."},
            },
            "required": ["subject", "value", "basis"],
        },
    )
    async def record_assumption(args: dict[str, Any]) -> dict[str, Any]:
        entry = assumptions.record(session.conversation_id, **args)
        session._publish("assumption.recorded", {
            "conversationId": session.conversation_id, **entry,
        })
        return {"content": [{"type": "text", "text":
            f"Recorded: {entry['subject']} = {entry['value']} {entry['unit']} ({entry['basis']})."}]}

    @tool(
        "show_chart",
        "Draw a chart in the conversation. Use it for any result the user should SEE "
        "rather than read as a table — a dispatch or price series over snapshots, a "
        "carrier mix, a cost or emissions breakdown, a comparison across scenarios. "
        "Pass data you already fetched; do not re-fetch to chart it. One chart per "
        "point you are making, with the sentence that interprets it — a chart with no "
        "claim attached is decoration. The chart the user gets is interactive: they "
        "can zoom the time axis, toggle series off, read every series at one snapshot, "
        "and flip it to a duration curve — so send the real resolution rather than "
        "pre-summarising, and prefer ONE stacked chart over several thin ones.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What the chart shows, in a few words."},
                "kind": {
                    "type": "string", "enum": ["line", "area", "bar", "scatter", "heatmap"],
                    "description": (
                        "line/area over snapshots or time; bar for categories; scatter for "
                        "one quantity against another; heatmap for a pattern across two "
                        "dimensions — for a heatmap each series is a ROW (e.g. one per day) "
                        "and x is the columns (e.g. hour of day)."
                    ),
                },
                "stacked": {
                    "type": "boolean",
                    "description": (
                        "Stack the series so they sum — the dispatch stack by carrier, "
                        "capacity by technology. Use it whenever the series are parts of a "
                        "whole; a stacked area against load is the canonical dispatch chart."
                    ),
                },
                "x": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Axis labels — snapshots for a series, category names for a bar.",
                },
                "series": {
                    "type": "array",
                    "description": "One entry per line/bar group.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "values": {
                                "type": "array", "items": {"type": ["number", "null"]},
                                "description": "One number per x; null for a gap (keeps its place).",
                            },
                            "color": {
                                "type": "string",
                                "description": "Optional CSS colour, e.g. the carrier's own colour.",
                            },
                            "axis": {
                                "type": "string", "enum": ["left", "right"],
                                "description": (
                                    "'right' for a series in different units on the same x — "
                                    "price in $/MWh over dispatch in MW. A right-axis series "
                                    "is never stacked."
                                ),
                            },
                            "kind": {
                                "type": "string", "enum": ["line", "area", "bar", "scatter"],
                                "description": "Override this one series, e.g. a price LINE over stacked areas.",
                            },
                        },
                        "required": ["name", "values"],
                    },
                },
                "unit": {"type": "string", "description": "MW, MWh, $/MWh, tCO2 … shown on the axis."},
                "unitRight": {"type": "string", "description": "Unit for the right-hand axis, if used."},
                "note": {"type": "string", "description": "Optional caveat shown under the chart."},
            },
            "required": ["title", "kind", "x", "series"],
        },
    )
    async def show_chart(args: dict[str, Any]) -> dict[str, Any]:
        # Published, not returned as data: the chart is for the USER, and echoing the
        # numbers back into the model's context would spend tokens re-reading what it
        # just sent. The confirmation is deliberately terse for the same reason.
        drawn = session._handle_show_chart(args)
        return {"content": [{"type": "text", "text": drawn}]}

    @tool(
        "show_in_ragnarok",
        "Point the user at a view in the Ragnarok tab — the map, the topology, a "
        "table, the results — instead of describing where to look. Emits a 'Show "
        "me' link in the conversation; clicking it switches tab and highlights "
        "what you named. Use it whenever your answer is about something SPATIAL or "
        "visual (a congested corridor, an isolated bus, where the new capacity "
        "landed), and after a solve so the user can confirm the result in the app "
        "they already know. It navigates only — it changes nothing.",
        {
            "type": "object",
            "properties": {
                "tab": {
                    "type": "string",
                    "enum": ["Model", "Build", "Data", "Forge", "Market", "PostAnalysis",
                             "Analytics", "PhysicalRisk", "Siting", "History", "Settings"],
                    "description": "Model = network map/table; PostAnalysis/Analytics = results.",
                },
                "select": {
                    "type": "string",
                    "description": "What to highlight there — a bus, line or generator NAME.",
                },
                "view": {
                    "type": "string", "enum": ["Map", "Table"],
                    "description": "Sub-view on the Model tab.",
                },
                "why": {
                    "type": "string",
                    "description": "One line: what the user should notice. Shown on the link.",
                },
            },
            "required": ["tab"],
        },
    )
    async def show_in_ragnarok(args: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "conversationId": session.conversation_id,
            "tab": str(args.get("tab") or "Model"),
            "select": str(args.get("select") or ""),
            "view": str(args.get("view") or ""),
            "why": str(args.get("why") or ""),
        }
        session._publish("show.request", payload)
        target = payload["select"] or payload["tab"]
        return {"content": [{"type": "text", "text": f"Offered the user a link to {target}."}]}

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
            session.conversation_id,
            str(args.get("path") or ""),
            offset=int(args.get("offset") or 0),
            limit=int(args.get("limit") or 200),
        )
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    bifrost_server = create_sdk_mcp_server(
        name="bifrost", version="1.0.0", tools=[ask_user, record_assumption, show_chart, show_in_ragnarok, read_attachment]
    )

    agent_auth = auth.resolve()
    mcp_env = {
        "PYTHONPATH": str(_REPO_ROOT),
        "RAGNAROK_API_BASE": API_BASE,
        "RAGNAROK_SESSION_ID": session.session_id,
        # Ragnarok's own confirm=true gate OFF: gating happens once, in can_use_tool
        # (see policy.py). Left on, an approved call would come back as a "preview".
        "RAGNAROK_MCP_AUTONOMY": "auto",
        # Bifrost owns the engine's lifecycle (engine.py). The MCP server's own
        # autostart would race it for the port, and stop the engine on exit.
        "RAGNAROK_MCP_AUTOSTART": "0",
        "RAGNAROK_MCP_TRANSPORT": "stdio",
        # Exports land in the agent's sandbox, not wherever the server was started.
        "RAGNAROK_MCP_FILE_DIR": str(session.workdir),
        "RAGNAROK_MCP_ACTOR": "agent",
        "RAGNAROK_MCP_CONVERSATION": session.conversation_id,
    }
    workdir = session.workdir
    workdir.mkdir(parents=True, exist_ok=True)

    # Ragnarok's MCP server is the agent's ENTIRE modelling toolset, and it lives in
    # the engine checkout — not here. It must therefore be launched with the
    # checkout's own interpreter (Bifrost's has no PyPSA) and with the checkout on
    # PYTHONPATH so `-m backend.mcp` resolves; the agent's own cwd is its file
    # sandbox and must not be repointed at the engine to achieve that.
    #
    # This used to be `sys.executable` + `-m backend.mcp` from Bifrost's workdir,
    # inherited from when the agent ran INSIDE Ragnarok. Once Bifrost became a
    # separate app with its package renamed to `bifrost/`, there was no `backend`
    # package to import: the server failed to start, every `mcp__ragnarok__*` tool
    # was silently absent, and the agent could only report that it had no way to
    # touch a model.
    mcp_servers: dict[str, Any] = {"bifrost": bifrost_server}
    engine_root = engine.load_root()
    engine_python = engine.find_interpreter(engine_root) if engine_root else None
    if engine_root is not None and engine_python is not None:
        mcp_servers["ragnarok"] = {
            "type": "stdio",
            "command": engine_python,
            "args": ["-m", "backend.mcp"],
            "env": {**mcp_env, "PYTHONPATH": str(engine_root)},
        }
    else:
        # Omitted rather than configured broken. A server that cannot start yields
        # no tools and no error the user can see; leaving it out lets
        # /api/agent/status say what is missing.
        logger.warning(
            "Ragnarok MCP server not configured — engine root %s, interpreter %s. "
            "The agent will have no modelling tools.", engine_root, engine_python,
        )

    # `effort` only exists on newer SDKs. Feature-detect rather than pass blindly:
    # an unknown kwarg is a TypeError at connect time, which would surface to the
    # user as a dead chat box rather than "your SDK is old".
    import dataclasses as _dc

    extra: dict[str, Any] = {}
    if "effort" in {f.name for f in _dc.fields(ClaudeAgentOptions)}:
        extra["effort"] = session.effort
    else:
        logger.warning(
            "claude_agent_sdk has no `effort` option; the effort picker will not "
            "take effect until the SDK is upgraded."
        )

    options = ClaudeAgentOptions(
        **extra,
        system_prompt=prompts.SYSTEM_PROMPT,
        mcp_servers=mcp_servers,
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
    effort = state.get("effort")
    if isinstance(effort, str) and effort.strip().lower() in EFFORT_CHOICES:
        session.effort = effort.strip().lower()
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


def peek_effort(session_id: str) -> str:
    """Selected effort without creating a session (live → persisted → default)."""
    session = _sessions.get(session_id)
    if session is not None:
        return session.effort
    effort = transcript.read_state(session_id).get("effort")
    if isinstance(effort, str) and effort.strip().lower() in EFFORT_CHOICES:
        return effort.strip().lower()
    return DEFAULT_EFFORT


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
