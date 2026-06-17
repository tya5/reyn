"""MCP server — expose Reyn agents to outer LLM clients.

This is the *server* side counterpart to :mod:`reyn.mcp.client` (which
*consumes* third-party MCP servers). External clients (Claude Code,
Cursor, OpenAI Agents SDK with MCP enabled, …) spawn ``reyn mcp serve``
as a stdio subprocess and converse with a Reyn agent through two tools:

  - ``reyn:list_agents()`` — enumerate registered agents.
  - ``reyn:send_to_agent(agent_name, message)`` — submit one user
    message to a named agent and block (with timeout) for the final
    reply text.

Multi-turn continuity falls out for free: ``Session.history`` is
persistent across calls because the registry caches each session
in-process and ``Session.load_history`` rehydrates from
``history.jsonl`` on construction.

FP-0013: ``send_to_agent_impl`` now drives ``session.run_one_iteration()``
via ``MessageBus.request`` rather than calling ``_handle_user_message``
inline.  Pumping from the same task eliminates the anyio stdio-starvation
failure mode (FP-0013 §ADR-A) and subsumes the previous tactical patches:
  - ``drain_skill_completed_inbox`` (R-A2A-COMPLETION-DRAIN)
  - ``running_plans`` manual gather (ADR-0023 §2.1.1)
  - ``running_skills`` manual gather (FP-0012)
These methods and attributes are retained for now (non-destructive migration)
and will be deleted in a future cleanup wave after ADR-A residual
verification (subprocess + real stdio probe / anyio CancelledError soak).

P7: tool names + tool semantics are OS-level (agent / message). No
skill-specific strings are baked in — what skills an agent runs in
response to a message is its own internal decision.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from reyn.chat.agent_locks import get_agent_lock as _get_agent_lock

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry
    from reyn.user_intervention import RequestBus


# Default time the server blocks waiting for the agent to finish a turn
# before returning whatever partial output has accumulated.
DEFAULT_SEND_TIMEOUT_SECONDS: float = 60.0

# Polling interval while waiting for the agent's run loop to drain its
# inbox + finish any spawned skills. Small enough that latency feels
# instant; large enough that the loop is essentially free.
_IDLE_POLL_INTERVAL_SECONDS: float = 0.05

# Brief grace period AFTER inbox is empty + skills are idle, before we
# declare the turn done. Without it we can race the router and miss the
# final ``kind="agent"`` outbox push.
_IDLE_GRACE_SECONDS: float = 0.05


# Per-agent serialization lock — shared across ALL transport layers (MCP +
# A2A).  ``_get_agent_lock`` is imported from ``reyn.chat.agent_locks`` at the
# top of this file so MCP and A2A share the SAME lock object per agent name.
# See that module for the full rationale.
#
# FP-0013: with MessageBus, the inbox is the serialization point but the
# lock is retained as a belt-and-suspenders measure during the migration
# period — it prevents concurrent calls from racing on history harvest
# (baseline → MessageBus.request → history-read must be atomic per agent).


async def _get_session(registry: "AgentRegistry", name: str) -> "object":
    """Return a loaded Session for `name`.

    Note: unlike `reyn chat`, the MCP path does NOT spawn a long-lived
    ``session.run()`` task. The MCP SDK's stdio transport (under
    anyio/asyncio) starves an `asyncio.create_task`-spawned background
    coroutine while the request handler is awaiting — the LLM call
    inside the agent never makes progress, the handler hits its
    timeout with an empty reply. Driving ``_handle_user_message``
    inline from the request handler keeps everything on the single
    event loop / task that the SDK is actively scheduling, and the
    LLM call awaits cleanly through to completion.
    """
    return registry.get_or_load(name)


def _new_agent_history_entries(
    session, baseline: int, *, chain_id: str | None = None,
) -> list[str]:
    """Return text of every history entry past `baseline` whose role is
    ``agent``. Order-preserving.

    When ``chain_id`` is provided, only entries whose ``meta["chain_id"]``
    matches are returned. This scopes reply harvesting to the caller's
    own chain so concurrent ``send_to_agent_impl`` calls (e.g. via the
    A2A FastAPI router) don't pick up each other's replies.
    """
    out: list[str] = []
    for msg in session.history[baseline:]:
        # Issue #383: role rename "agent" → "assistant"; tolerate both.
        if msg.role not in ("assistant", "agent") or not msg.text:
            continue
        if chain_id is not None and (msg.meta or {}).get("chain_id") != chain_id:
            continue
        out.append(msg.text)
    return out


async def _await_turn_complete(
    session, *, baseline: int, timeout: float
) -> bool:
    """Wait until the agent has produced at least one new ``role="agent"``
    history entry past ``baseline`` AND the run loop is back to idle
    (inbox empty + no running skills). Returns True on completion, False
    on timeout.

    The negative-signal-only approach (= "looks idle, no work in flight")
    used to false-positive: between ``submit_user_text`` returning and
    the run loop's ``inbox.get()`` actually picking up the message,
    there's a window where the inbox briefly looks empty even though
    nothing has been processed yet. Adding the positive signal (= a
    new agent reply landed in history) closes that race — we only
    declare done once the agent has measurably emitted something AND
    the run loop has parked.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        # Issue #383: assistant replies now have role="assistant". "agent"
        # kept in the predicate for any pre-#383 history entry that
        # bypassed read-time migration.
        has_reply = any(
            msg.role in ("assistant", "agent")
            for msg in session.history[baseline:]
        )
        is_idle = session.inbox.empty() and not session.running_skills
        if has_reply and is_idle:
            # Grace period to absorb a possible second `agent_response`
            # follow-up in the same turn (= multi-iteration router loops).
            await asyncio.sleep(_IDLE_GRACE_SECONDS)
            if session.inbox.empty() and not session.running_skills:
                return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(_IDLE_POLL_INTERVAL_SECONDS)


async def list_agents_impl(registry: "AgentRegistry") -> list[dict]:
    """Backing implementation of the ``list_agents`` tool.

    Separated from the SDK glue so the unit tests can call it directly
    without spinning up a stdio transport.
    """
    out: list[dict] = []
    for name in registry.list_names():
        try:
            profile = registry.load_profile(name)
            role = (profile.role or "").strip().splitlines()
            role_excerpt = role[0].strip() if role else ""
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning("list_agents: profile load failed for %r: %s", name, e)
            role_excerpt = ""
        out.append({"name": name, "role": role_excerpt})
    return out


async def send_to_agent_impl(
    registry: "AgentRegistry",
    *,
    agent_name: str,
    message: str,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    intervention_override: "RequestBus | None" = None,
) -> dict:
    """Backing implementation of the ``send_to_agent`` tool.

    Returns a dict shaped::

        {"reply": str, "partial": bool, "agent": str}

    where ``partial=True`` indicates the timeout fired before the agent
    went idle. The agent's task is NOT cancelled in that case — its
    work is preserved on the inbox / running_skills, and the next
    ``send_to_agent`` call (or ``reyn chat`` attach) will see the rest
    of the work as it lands in history.

    FP-0013: uses ``MessageBus.request`` to pump ``session.run_one_iteration``
    from this task, eliminating the inline ``_handle_user_message`` bypass and
    the tactical drains (``drain_skill_completed_inbox``, ``running_plans``
    gather, ``running_skills`` gather).  The inbox is now the single intake
    channel for every transport surface.
    """
    if not registry.exists(agent_name):
        raise ValueError(
            f"agent {agent_name!r} not found; "
            f"create it with `reyn agent new {agent_name}`"
        )

    session = await _get_session(registry, agent_name)

    from reyn.chat.message_bus import MessageBus  # noqa: PLC0415 — lazy import
    from reyn.chat.session import _new_chain_id  # noqa: PLC0415 — lazy import
    from reyn.chat.transport import McpRef  # noqa: PLC0415 — lazy import

    chain_id = _new_chain_id()
    req_id = f"mcp-{chain_id}"

    # Serialize concurrent calls to the same agent — the lock keeps
    # baseline → MessageBus.request → history-read atomic per agent.
    async with _get_agent_lock(agent_name):
        baseline = len(session.history)
        bus = MessageBus()
        # issue #268 Phase 2: when the override exposes a stable
        # ``channel_id`` (= A2AInterventionBus does), register it as
        # an intervention listener so the agent layer's origin-pin
        # check (= ``Session.handle_intervention`` Branch 3)
        # treats the A2A channel as alive while the bus is active.
        # ``getattr`` lets future buses without channel_id participate
        # via the override path without forcing them to expose one.
        override_channel_id: str | None = None
        if intervention_override is not None:
            session.register_intervention_override(chain_id, intervention_override)
            override_channel_id = getattr(
                intervention_override, "channel_id", None,
            )
            if override_channel_id is not None:
                session.register_intervention_listener(override_channel_id)
        try:
            replies = await bus.request(
                session,
                kind="user",
                payload={"text": message, "chain_id": chain_id},
                reply_to=McpRef(request_id=req_id),
                timeout=timeout,
            )
        finally:
            if intervention_override is not None:
                session.unregister_intervention_override(chain_id)
                if override_channel_id is not None:
                    session.unregister_intervention_listener(override_channel_id)
        new_replies = _new_agent_history_entries(
            session, baseline, chain_id=chain_id,
        )

    # idle = MessageBus returned quiescently (all tasks done, inbox empty).
    # We use history-based reply harvest for backward compat with chain_id
    # filtering (outbox reply_to stamping is not yet universal).
    idle = _is_quiescent_after_bus(session)
    reply_text = "\n\n".join(new_replies).strip()

    if not reply_text:
        # Fall back to outbox-collected text if history harvest is empty
        # (e.g. when monkeypatched handlers write to outbox but not history).
        outbox_texts = [r.text for r in replies if r.text]
        reply_text = "\n\n".join(outbox_texts).strip()

    if not idle and not reply_text:
        reply_text = (
            f"(agent {agent_name!r} is still working; "
            f"no reply emitted within {timeout:.0f}s — "
            "its task continues in the background; call again to receive the rest.)"
        )

    # B42-NF-W6-2: surface still-running skill run_ids so the A2A sync
    # path can auto-escalate to a Task envelope (= A2A spec-compliant
    # async response) when the timeout fires before quiescence. Empty
    # list when no skill is in flight (the common case).
    running_skill_run_ids: list[str] = []
    if not idle:
        running_skills_attr: dict = getattr(session, "running_skills", {})
        for rid, task in running_skills_attr.items():
            if not task.done():
                running_skill_run_ids.append(rid)

    # #1649 PART B: detect a limit-abort. The router stamps ``limit_stopped`` on
    # the limit wrap-up / degrade outbox message. A non-TTY run-once / wrapper
    # caller uses this to (a) surface the decision-enabling message even when the
    # history harvest is empty (kind="error" isn't persisted to history) and
    # (b) exit NON-ZERO — so a limit hit is never a silent exit-0 stop.
    limit_stopped = any(
        isinstance(getattr(r, "meta", None), dict) and r.meta.get("limit_stopped")
        for r in replies
    )
    if limit_stopped and not reply_text:
        _limit_texts = [
            r.text for r in replies
            if isinstance(getattr(r, "meta", None), dict)
            and r.meta.get("limit_stopped") and r.text
        ]
        reply_text = "\n\n".join(_limit_texts).strip() or reply_text

    return {
        "reply": reply_text,
        "partial": (not idle),
        "agent": agent_name,
        "running_skill_run_ids": running_skill_run_ids,
        "limit_stopped": limit_stopped,
    }


def _is_quiescent_after_bus(session) -> bool:
    """Check if the session is quiescent after MessageBus.request returns.

    MessageBus already waited for quiescence; this is a final check
    that captures the partial=True case (timeout fired before quiescence).
    """
    if not session.inbox.empty():
        return False
    running_skills: dict = getattr(session, "running_skills", {})
    if any(not t.done() for t in running_skills.values()):
        return False
    running_plans: dict = getattr(session, "running_plans", {})
    if any(not t.done() for t in running_plans.values()):
        return False
    return True


# ── SDK glue ────────────────────────────────────────────────────────────────


def build_server(
    registry: "AgentRegistry",
    *,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
):
    """Construct an ``mcp.server.Server`` wired to the given registry.

    Imports of the ``mcp`` SDK are deferred so the module itself can be
    imported in test environments where ``mcp`` is not installed (the
    tests of this module install it via the ``mcp`` extra; the rest of
    the suite doesn't touch this surface).
    """
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("reyn")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # type: ignore[no-redef]
        return [
            Tool(
                name="list_agents",
                description=(
                    "List the agents registered in the current Reyn project. "
                    "Returns each agent's name and a short role excerpt."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="send_to_agent",
                description=(
                    "Send a single user-style message to a named Reyn agent "
                    "and return its reply text. The agent decides internally "
                    "what skills (if any) to run; multi-turn conversation "
                    "accumulates because per-agent chat history persists."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": (
                                "Name of the agent to send to. Use list_agents "
                                "to enumerate."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "description": "User message body.",
                        },
                    },
                    "required": ["agent_name", "message"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="answer_intervention",
                description=(
                    "Deliver an answer to a pending ask_user / "
                    "permission / safety intervention on a running "
                    "send_to_agent call (issue #270 Phase B). "
                    "Routes via Session.answer_pending_intervention. "
                    "Identify the iv by ``run_id`` (= surfaced in the "
                    "progress notification that the server pushed when "
                    "the iv was dispatched, see experimental capability "
                    "``reyn.iv.input_required``). For choice-based "
                    "prompts (= permission.* / safety.limit.*) pass "
                    "``choice_id`` explicitly; for free-text ask_user "
                    "omit it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": (
                                "Name of the agent that emitted the "
                                "intervention (= the same agent_name "
                                "used in the original send_to_agent "
                                "call)."
                            ),
                        },
                        "run_id": {
                            "type": "string",
                            "description": (
                                "The iv's run_id, as surfaced in the "
                                "input-required progress notification."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "Free-text answer body. For choice-"
                                "based prompts, set ``choice_id`` "
                                "below; the text becomes the human-"
                                "readable selection label."
                            ),
                        },
                        "choice_id": {
                            "type": "string",
                            "description": (
                                "Optional. For closed-set prompts, "
                                "the explicit choice id from the "
                                "iv's choices list (e.g. ``yes`` / "
                                "``always`` / ``no``). Omit for "
                                "free-text ask_user answers."
                            ),
                        },
                    },
                    "required": ["agent_name", "run_id", "text"],
                    "additionalProperties": False,
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(  # type: ignore[no-redef]
        name: str, arguments: dict,
    ) -> list[TextContent]:
        if name == "list_agents":
            agents = await list_agents_impl(registry)
            import json
            return [TextContent(type="text", text=json.dumps(agents))]

        if name == "send_to_agent":
            agent_name = (arguments or {}).get("agent_name") or ""
            message = (arguments or {}).get("message") or ""
            if not agent_name:
                return [TextContent(type="text", text="error: agent_name is required")]
            if not message:
                return [TextContent(type="text", text="error: message is required")]

            # issue #271 M1: progress emit bridge — if the client provided
            # a progressToken in this request's metadata, subscribe a
            # bridge to the agent's chat_events EventLog that translates
            # lifecycle events into ``notifications/progress`` messages
            # so the peer (= Reyn-as-MCP-client) can render "what is the
            # server doing right now" instead of waiting silently.
            #
            # M1-b lifecycle event scope per owner decision: phase
            # transitions + LLM round + act batch completion. Skipping
            # high-volume / low-info events keeps the channel useful.
            #
            # Cleanup: ``finally`` removes the subscriber regardless of
            # how the handler exits (= normal return / ValueError /
            # CancelledError from issue #271 M2 client-side cancel).
            bridge = await _make_mcp_progress_bridge(
                registry, agent_name, server,
            )
            # issue #270 Phase B: build MCP-side iv observer. When a
            # skill emits a UserIntervention, this bus pushes the
            # iv payload to the peer via progress notification + lets
            # the peer answer via the ``answer_intervention`` tool.
            iv_bus = await _make_mcp_intervention_bus(
                registry, agent_name, server,
            )
            try:
                result = await send_to_agent_impl(
                    registry,
                    agent_name=agent_name,
                    message=message,
                    timeout=timeout,
                    intervention_override=iv_bus,
                )
            except ValueError as e:
                return [TextContent(type="text", text=f"error: {e}")]
            except asyncio.CancelledError:
                # issue #271 M2: client sent CancelledNotification; the
                # SDK has already cancelled the responder. Re-raise so
                # the SDK's cancellation suppression kicks in (= no
                # duplicate error response). The bridge teardown in
                # finally still runs.
                raise
            finally:
                if bridge is not None:
                    bridge.detach()
            import json
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "answer_intervention":
            import json  # noqa: PLC0415

            from reyn.user_intervention import InterventionAnswer  # noqa: PLC0415

            args = arguments or {}
            agent_name = args.get("agent_name") or ""
            run_id = args.get("run_id") or ""
            text = args.get("text") or ""
            choice_id_raw = args.get("choice_id")
            choice_id: str | None = (
                choice_id_raw if isinstance(choice_id_raw, str) and choice_id_raw
                else None
            )
            if not agent_name:
                return [TextContent(type="text", text="error: agent_name is required")]
            if not run_id:
                return [TextContent(type="text", text="error: run_id is required")]
            if not registry.exists(agent_name):
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "answered": False,
                        "reason": f"agent {agent_name!r} not found",
                    }),
                )]
            try:
                session = await _get_session(registry, agent_name)
            except Exception as exc:  # noqa: BLE001 — defensive
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "answered": False,
                        "reason": f"agent load failed: {exc}",
                    }),
                )]
            answer = InterventionAnswer(text=text, choice_id=choice_id)
            delivered = await session.answer_pending_intervention(run_id, answer)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "answered": bool(delivered),
                    "reason": (
                        None if delivered else
                        "already answered or no pending intervention"
                    ),
                }),
            )]

        return [TextContent(type="text", text=f"error: unknown tool {name!r}")]

    # ── resources/read (#385 β core impl sub-task 3d) ──────────────────
    #
    # External MCP clients (= Claude Desktop, Cursor, ...) receive path-
    # refs in tool responses (= ``send_to_agent`` returning a result
    # body that contains a ``reyn-tool-result://`` URI). When the client
    # wants the full body, it calls ``resources/read(uri=...)``. This
    # handler resolves the vendor-scheme URI to the local file via the
    # same MediaStore boundary check as ``read_tool_result`` — same-
    # host fs read, no HTTP indirection because the MCP server already
    # IS on the producing host.

    @server.read_resource()
    async def _read_resource(uri):  # type: ignore[no-redef]
        """Resolve a ``reyn-tool-result://<agent>/<artifact>`` URI to its
        body for cross-protocol consumers (= external MCP clients).

        Returns ``[ReadResourceContents(...)]`` — the SDK's non-
        deprecated shape. Unsupported URI schemes and missing files
        surface as ``text/plain`` content with an ``error: ...`` body
        so the client still gets a structured response rather than a
        transport error. Path-traversal escapes propagate as
        PermissionError (= the MCP framework wraps as an error to the
        client).
        """
        from mcp.server.lowlevel.helper_types import ReadResourceContents

        from reyn.data.workspace.media_store import (
            MediaStore,
            MediaStoreConfig,
            parse_resource_uri,
        )
        uri_str = str(uri)
        parsed = parse_resource_uri(uri_str)
        if parsed is None:
            # Unsupported URI scheme (= not ``reyn-tool-result://...``).
            # External MCP clients sometimes pass other schemes (= file://,
            # https://); we explicitly only resolve our vendor scheme via
            # this handler. For https:// URLs, the client should fetch
            # directly (= the URL points at our own resources router).
            return [ReadResourceContents(
                content=(
                    f"error: unsupported resource URI scheme: {uri_str!r}. "
                    "Reyn MCP server resolves reyn-tool-result://<agent>/<artifact> only; "
                    "for https:// URLs fetch directly via the resources router."
                ),
                mime_type="text/plain",
            )]
        agent_name, _artifact = parsed
        # MediaStore is per-agent-identity, but the file is in a shared
        # tool_results_dir. Construct ad-hoc with the URI-claimed agent
        # name; the boundary check ensures the resolved path stays
        # inside ``.reyn/tool-results/`` regardless.
        from pathlib import Path
        store = MediaStore(
            MediaStoreConfig(),
            project_root=Path.cwd(),
            agent_name=agent_name,
        )
        body, found = store.read_tool_result_by_uri(uri_str)
        if not found:
            return [ReadResourceContents(
                content=(
                    f"error: tool result not found for URI {uri_str!r} "
                    "(= deleted by user, or never existed on this Reyn instance)"
                ),
                mime_type="text/plain",
            )]
        return [ReadResourceContents(content=body, mime_type="text/plain")]

    return server


async def _make_mcp_intervention_bus(
    registry: "AgentRegistry",
    agent_name: str,
    server: "object",
) -> "_MCPInterventionBus | None":
    """Build an MCP iv-observer for the duration of one ``send_to_agent``
    call (issue #270 Phase B).

    issue #292 α extended to MCP: when a skill spawned via
    ``send_to_agent_impl`` emits a ``UserIntervention``, that iv lands
    in ``Session._interventions._active`` and ``handler.dispatch``
    awaits its future. Pre-#270 Phase B the MCP transport had no
    observer registered as chain override → no peer-facing surface to
    push the iv question to → the iv would hang if no TUI was
    simultaneously attached.

    This bus fills the same role ``A2AInterventionBus`` does for the
    A2A surface: pure side-effect observer (= ``on_dispatch(iv)``
    pushes an MCP notification carrying the iv payload; does NOT await
    ``iv.future``). The peer answers via a separate
    ``answer_intervention`` MCP tool call that lands at
    ``Session.answer_pending_intervention``.

    Returns ``None`` when the request context is unavailable (= e.g.
    direct test calls bypassing the MCP server). The caller (= send-
    to-agent handler) then runs without the override, matching pre-
    Phase-B behaviour.
    """
    try:
        ctx = server.request_context  # type: ignore[attr-defined]
    except (LookupError, AttributeError):
        return None
    if not registry.exists(agent_name):
        return None
    return _MCPInterventionBus(
        mcp_session=ctx.session,
        related_request_id=ctx.request_id,
    )


class _MCPInterventionBus:
    """MCP-side iv side-effect observer (issue #270 Phase B).

    Registered as the chain-scoped override during ``send_to_agent_impl``.
    Mirrors ``A2AInterventionBus``'s post-α observer shape:

      - ``on_dispatch(iv)`` runs as a side effect inside
        ``Session._dispatch_intervention``, BEFORE the regular
        handler dispatch awaits ``iv.future``.
      - Stamps ``iv.origin_channel_id`` so the agent layer can attribute
        this iv to the MCP channel.
      - Pushes an iv-payload notification to the MCP peer (= the
        client that opened the ``send_to_agent`` request) so the
        peer's UI can render the question + collect the answer.
      - Does NOT await ``iv.future``. The handler awaits on the
        skill's behalf; the peer answers via the
        ``answer_intervention`` MCP tool which routes to
        ``Session.answer_pending_intervention``.

    Notification transport: uses ``Session.send_progress_notification``
    with the iv payload encoded as JSON in the ``message`` field +
    ``progress=0.0`` / ``total=None`` (= indeterminate, per MCP spec
    for non-numeric updates). This piggy-backs on the existing
    progress channel rather than introducing a new notification type
    — clients that already parse progress messages from PR #279's
    ``_MCPProgressBridge`` see the iv as one more structured payload
    with a recognisable ``{"type": "intervention", ...}`` shape.

    The Reyn experimental capability ``reyn.iv.input_required``
    (declared in ``serve_stdio``) advertises this shape to peers via
    the MCP ``initialize`` response.
    """

    def __init__(
        self,
        *,
        mcp_session: "object",
        related_request_id: "str | None",
    ) -> None:
        self._mcp_session = mcp_session
        self._related_request_id = related_request_id

    @property
    def channel_id(self) -> str:
        """Stable channel identifier for issue #268 origin-pin routing.

        Format: ``mcp:<request_id>``. The bus's lifetime is one
        ``send_to_agent`` MCP call, so the channel id is unique per
        call.
        """
        return f"mcp:{self._related_request_id}"

    async def on_dispatch(self, iv) -> None:
        """Side-effect observer entry point.

        Stamp the iv's ``origin_channel_id`` (= for #268 cross-channel
        routing), build the canonical input-required payload (= same
        shape PR #285 Gap 4 standardised for A2A), and push it as a
        progress notification. Failures are swallowed — the handler's
        dispatch path must continue regardless of whether the peer
        actually received the notification.
        """
        if iv.origin_channel_id is None:
            iv.origin_channel_id = self.channel_id

        payload = {
            "type": "intervention",
            "status": "input-required",
            "run_id": iv.run_id,
            "kind": iv.kind,
            "question": iv.prompt,
            "choices": [
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in iv.choices
            ],
        }
        if iv.detail:
            payload["detail"] = iv.detail

        send_fn = getattr(
            self._mcp_session, "send_progress_notification", None,
        )
        if send_fn is None:
            return
        import json  # noqa: PLC0415

        try:
            await send_fn(
                progress_token=f"reyn-iv:{iv.id}",
                progress=0.0,
                total=None,
                message=json.dumps(payload),
                related_request_id=self._related_request_id,
            )
        except Exception:  # noqa: BLE001 — best-effort
            return


async def _make_mcp_progress_bridge(
    registry: "AgentRegistry",
    agent_name: str,
    server: "object",
) -> "_MCPProgressBridge | None":
    """Build a progress-forwarding bridge for the duration of one
    ``send_to_agent`` call (issue #271 M1).

    Returns ``None`` when:
      - the client didn't set ``_meta.progressToken`` on this request
        (= peer doesn't care about progress, save the work)
      - the agent ``agent_name`` doesn't exist (= the caller's
        existence check in ``_call_tool`` will produce the standard
        error path; we silently no-op here)
      - the request context is unavailable for any reason (= defensive)

    The returned bridge has subscribed itself to the agent's chat
    events; callers MUST call ``bridge.detach()`` in a ``finally`` to
    avoid the subscriber leaking across calls.
    """
    try:
        ctx = server.request_context  # type: ignore[attr-defined]
    except (LookupError, AttributeError):
        return None
    if ctx.meta is None or ctx.meta.progressToken is None:
        return None
    if not registry.exists(agent_name):
        return None
    try:
        session = await _get_session(registry, agent_name)
    except Exception:  # noqa: BLE001 — defensive, never block the main call
        return None
    bridge = _MCPProgressBridge(
        session=session,
        mcp_session=ctx.session,
        progress_token=ctx.meta.progressToken,
        related_request_id=ctx.request_id,
    )
    bridge.attach()
    return bridge


class _MCPProgressBridge:
    """Forwards selected agent chat-events to MCP progress notifications.

    issue #271 M1 (= M1-b lifecycle event scope per owner decision):

      - ``phase_started`` → progress = next ordinal, message = "phase: <name>"
      - ``llm_called`` → progress = ordinal, message = "llm: <model>"
      - ``act_executed`` → progress = ordinal, message = "act: <N> op(s)"

    ``progress`` is monotonic (= ordinal counter) since we don't have a
    meaningful total. The MCP spec accepts ``total=None`` for
    indeterminate progress; clients render as raw value or spinner.

    The subscriber runs synchronously in the EventLog dispatcher; it
    schedules the actual ``send_progress_notification`` as an asyncio
    task so we don't block the event emitter on the MCP transport. Any
    transport / cancellation error in the background task is swallowed
    (= progress is best-effort; the main call must never fail because
    notification delivery failed).
    """

    TRACKED_EVENTS = frozenset({
        "phase_started",
        "llm_called",
        "act_executed",
    })

    def __init__(
        self,
        *,
        session: "object",
        mcp_session: "object",
        progress_token: "str | int",
        related_request_id: "str | None",
    ) -> None:
        self._session = session
        self._mcp_session = mcp_session
        self._progress_token = progress_token
        self._related_request_id = related_request_id
        self._ordinal = 0
        self._detached = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def detached(self) -> bool:
        """Read-only accessor for the bridge's detached flag.

        Symmetric with ``_A2AProgressBridge.detached``; tests verify the
        attach / detach lifecycle via this surface.
        """
        return self._detached

    def attach(self) -> None:
        events = getattr(self._session, "_chat_events", None)
        if events is not None:
            events.add_subscriber(self._on_event)

    def detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        events = getattr(self._session, "_chat_events", None)
        if events is not None:
            events.remove_subscriber(self._on_event)
        # Best-effort: cancel in-flight notification tasks so they don't
        # outlive the request.
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def _on_event(self, event: "object") -> None:
        # Sync callback from EventLog dispatcher. Filter by type, build
        # the message, schedule async send.
        if self._detached:
            return
        event_type = getattr(event, "type", None)
        if event_type not in self.TRACKED_EVENTS:
            return
        data = getattr(event, "data", {}) or {}
        message = self._format_message(event_type, data)
        self._ordinal += 1
        ordinal = float(self._ordinal)
        try:
            task = asyncio.ensure_future(self._send(ordinal, message))
        except RuntimeError:
            # No running loop (= EventLog dispatched outside async context).
            # Skip — caller will see this event later if/when an async
            # context picks up the next event.
            return
        self._tasks.append(task)

    @staticmethod
    def _format_message(event_type: str, data: dict) -> str:
        if event_type == "phase_started":
            phase = data.get("phase") or "?"
            return f"phase: {phase}"
        if event_type == "llm_called":
            model = data.get("model") or "?"
            return f"llm: {model}"
        if event_type == "act_executed":
            op_count = data.get("op_count") or 0
            suffix = "" if op_count == 1 else "s"
            return f"act: {op_count} op{suffix}"
        return event_type

    async def _send(self, ordinal: float, message: str) -> None:
        send_fn = getattr(self._mcp_session, "send_progress_notification", None)
        if send_fn is None:
            return
        try:
            await send_fn(
                progress_token=self._progress_token,
                progress=ordinal,
                total=None,
                message=message,
                related_request_id=self._related_request_id,
            )
        except Exception:  # noqa: BLE001 — progress is best-effort
            # Any transport failure / cancellation: silently drop.
            # The main send_to_agent call must not fail because we
            # couldn't push a progress notification.
            return


async def serve_stdio(
    registry: "AgentRegistry",
    *,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
) -> None:
    """Run the MCP server speaking JSON-RPC over stdio until EOF / SIGINT.

    On exit, the registry is shut down so any in-flight chat sessions
    drain cleanly (mirrors what ``reyn chat`` does on quit).
    """
    from mcp.server.stdio import stdio_server

    server = build_server(registry, timeout=timeout)
    # issue #271 M3: capability advertising. Declare what this server
    # actually emits + handles so MCP clients can negotiate features
    # before issuing send_to_agent calls. Reality must match the claim
    # (= avoid the #267 Z-b "capability claim vs reality" mismatch
    # pattern by deriving each entry from a concrete production wire):
    #
    #   - NotificationOptions: tools/prompts/resources lists are STATIC
    #     (= ``_list_tools`` returns the same 2 tools every call, no
    #     notify_list_changed call sites in src/reyn/mcp_server.py).
    #   - experimental ``reyn.progress.skill_lifecycle``: PR #279 wired
    #     ``_MCPProgressBridge`` to subscribe chat_events + emit
    #     ``notifications/progress`` for phase_started / llm_called /
    #     act_executed during send_to_agent.
    #   - experimental ``reyn.cancellation.cooperative``: PR #279 wired
    #     ``notifications/cancelled`` propagation through
    #     asyncio.CancelledError → in-flight skill interruption.
    from mcp.server import NotificationOptions

    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(
            prompts_changed=False,
            resources_changed=False,
            tools_changed=False,
        ),
        experimental_capabilities={
            "reyn.progress.skill_lifecycle": {
                "version": 1,
                "events": [
                    "phase_started",
                    "llm_called",
                    "act_executed",
                ],
            },
            "reyn.cancellation.cooperative": {
                "version": 1,
            },
            "reyn.iv.input_required": {
                "version": 1,
                "transport": "progress_notification",
                "message_format": "json",
                "shape": {
                    "type": "intervention",
                    "status": "input-required",
                    "run_id": "<string>",
                    "kind": "<ask_user|permission.*|safety.limit.*>",
                    "question": "<string>",
                    "choices": "<list of {id,label,hotkey}>",
                    "detail": "<optional string>",
                },
                "answer_tool": "answer_intervention",
            },
        },
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        try:
            await registry.shutdown()
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("registry shutdown after MCP serve: %s", e)


__all__ = [
    "build_server",
    "list_agents_impl",
    "send_to_agent_impl",
    "serve_stdio",
    "DEFAULT_SEND_TIMEOUT_SECONDS",
]
