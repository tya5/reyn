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
failure mode (FP-0013 §ADR-A).

P7: tool names + tool semantics are OS-level (agent / message). No
domain-specific strings are baked in — how the agent handles a message
is its own internal decision.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from reyn.core.events.progress_lifecycle import (
    PROGRESS_LIFECYCLE_EVENTS,
    format_progress_message,
)
from reyn.runtime.agent_locks import get_agent_lock as _get_agent_lock
from reyn.runtime.session_pure import new_chain_id
from reyn.runtime.turn_origin import TurnOrigin

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from reyn.mcp.extra_tool import ExtraTool
    from reyn.runtime.registry import AgentRegistry
    from reyn.user_intervention import RequestBus


# Default time the server blocks waiting for the agent to finish a turn
# before returning whatever partial output has accumulated.
DEFAULT_SEND_TIMEOUT_SECONDS: float = 60.0


# Per-(agent, sid) serialization lock (proposal 0067 P1, #3978 — rekeyed
# from agent_name alone; this lock protects THIS session's history, and an
# agent can have more than one live session). ``_get_agent_lock`` is imported
# from ``reyn.runtime.agent_locks`` at the top of this file; A2A traffic
# reaches this same lock by routing through ``send_to_agent_impl`` below
# rather than acquiring its own (see that module's docstring for the full
# rationale).
#
# FP-0013: with MessageBus, the inbox is the serialization point but the
# lock is retained as a belt-and-suspenders measure during the migration
# period — it prevents concurrent calls from racing on history harvest
# (baseline → MessageBus.request → history-read must be atomic per agent).


async def _get_session(
    registry: "AgentRegistry", name: str, *, sid: "str | None" = None,
) -> "object":
    """Return a loaded Session for `name` (the agent's "main" session by default).

    FP-0043 S4b-4: when ``sid`` is given (a per-delegation ``a2a:<id>`` session the
    a2a router already spawned via resolve_session), return THAT session so the
    delegation runs isolated from "main". The session needs no run-loop — the
    caller drives the turn inline via ``MessageBus.request`` — so this is a plain
    lookup, mirroring the no-background-task note below. Falls back to "main" when
    the sid is absent or unknown.

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
    if sid is not None and sid != "main":
        existing = registry.get_session(name, sid)
        if existing is not None:
            return existing
    return registry.get_or_load(name)


def _history_baseline_seq(session) -> int:
    """The ``seq`` watermark to capture BEFORE dispatching a request, so
    ``_new_agent_history_entries`` can later ask "what's new since then" by
    coordinate rather than by list position (#4387 architect review on
    Phase B ②: ``session.history[baseline:]`` — a captured ``len()`` —
    silently returns the WRONG (older) slice once anything can prepend to
    ``self.history``, which Phase B's on-demand older-entry loading will do
    for other consumers; this reads the wrong thing without raising, so it
    would ship broken).

    Mirrors ``load_history``'s own ``max(seq)`` derivation rather than just
    reading the last entry's ``seq`` — safe even for a session whose most
    recent entry happens to be legacy ``seq == 0`` data (pre-#3704): any
    newly-appended entry always gets a real ``seq`` strictly greater than
    every ``seq`` that already exists, so the max over everything currently
    held is always a valid watermark, regardless of ordering quirks in old
    data.
    """
    return max((m.seq for m in session.history if m.seq), default=0)


def _new_agent_history_entries(
    session, baseline_seq: int, *, chain_id: str | None = None,
) -> list[str]:
    """Return text of every history entry with ``seq > baseline_seq`` whose
    role is ``agent``. Order-preserving (``self.history`` is append-ordered
    by construction; a coordinate-based filter over it stays in that order).

    When ``chain_id`` is provided, only entries whose ``meta["chain_id"]``
    matches are returned. This scopes reply harvesting to the caller's
    own chain so concurrent ``send_to_agent_impl`` calls (e.g. via the
    A2A FastAPI router) don't pick up each other's replies.

    #4387 Phase B ③ re-audit (checked, not assumed — lead-coder's explicit
    instruction: "prepend で通る ≠ evict で通る"): ``self.history`` can now
    also shrink from the FRONT (oldest-first eviction, bounding resident
    memory — the symmetric, opposite-direction operation of the #4404
    prepend fix this function's ``seq``-based filter was originally built
    for). Eviction removes only the entries with the LOWEST ``seq`` values
    currently resident, so the common case — normal turn-by-turn growth
    evicting entries OLDER than any live ``baseline_seq`` — is safe by the
    same construction #4404 already relies on (a coordinate filter, immune
    to ANY reshuffling of position, prepend or evict alike).

    One genuine, narrow limitation this re-audit surfaced (see
    ``tests/runtime/test_4387_history_resident_eviction.py::
    test_mcp_baseline_harvest_narrow_gap_when_the_harvest_window_itself_exceeds_the_cap``):
    if growth BETWEEN capturing ``baseline_seq`` and calling this function
    itself exceeds the resident byte cap, eviction can remove even
    post-baseline entries (whichever are oldest among what's resident at
    eviction time) — this function would then under-report, silently
    missing an early reply from a very large harvest window. Inherent to
    any byte-bounded resident cache, not a defect fixable here without
    unbounding the cache entirely (which would reopen #4387's own reason
    for existing) — documented as a checked, honest boundary rather than
    assumed safe.
    """
    out: list[str] = []
    for msg in session.history:
        if msg.seq <= baseline_seq:
            continue
        # Issue #383: role rename "agent" → "assistant"; tolerate both.
        if msg.role not in ("assistant", "agent") or not msg.text:
            continue
        if chain_id is not None and (msg.meta or {}).get("chain_id") != chain_id:
            continue
        out.append(msg.text)
    return out


async def list_agents_impl(registry: "AgentRegistry") -> list[dict]:
    """Backing implementation of the ``list_agents`` tool.

    Separated from the SDK glue so the unit tests can call it directly
    without spinning up a stdio transport.
    """
    out: list[dict] = []
    for name in registry.list_active_names():  # #1954: hide archived agents
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
    sid: "str | None" = None,
    # TurnOrigin is imported at module level, not with this module's usual
    # lazy-import idiom, because a default argument is evaluated at def time.
    # reyn.runtime.turn_origin imports nothing but enum.
    inbox_kind: TurnOrigin = TurnOrigin.EXTERNAL_MESSAGE,
) -> dict:
    """Backing implementation of the ``send_to_agent`` tool.

    Returns a dict shaped::

        {"reply": str, "partial": bool, "agent": str}

    where ``partial=True`` indicates the timeout fired before the agent
    went idle. The agent's task is NOT cancelled in that case — its
    work is preserved on the inbox, and the next
    ``send_to_agent`` call (or ``reyn chat`` attach) will see the rest
    of the work as it lands in history.

    FP-0013: uses ``MessageBus.request`` to pump ``session.run_one_iteration``
    from this task, eliminating the inline ``_handle_user_message`` bypass.
    The inbox is now the single intake channel for every transport surface.

    ``inbox_kind`` (#3595 step 1b) is the ``TurnOrigin`` member ``message``
    rides onto the inbox, and it exists because THIS function has producers with
    two different answers to "who wrote this text":

    - the MCP ``send_to_agent`` tool and the A2A JSON-RPC router — a
      counterparty outside this process, frequently another LLM. They take the
      default, ``TurnOrigin.EXTERNAL_MESSAGE``.
    - first-party operator surfaces, which pass ``TurnOrigin.CLIENT_INPUT``
      explicitly. ★ Which callers those are, and why each is entitled to that
      claim, is NOT counted here: it is enumerated once, in
      ``tests/runtime/test_3595_client_input_provenance_gate.py``'s allowlist, which is
      a gate rather than prose — this docstring said "Both pass …" while there
      were three such call sites, because a count in prose is a snapshot of
      whoever last read the code.

    The default is the NON-operator one on purpose: a new caller that says
    nothing gets the kind that cannot execute a slash command, the same
    fail-safe direction ``Session._stamp_execution_context`` uses for turn
    origin. Under the old unconditional ``kind="user"`` an MCP client could run
    any registered slash command by sending ``/reset`` as its message.
    """
    if not registry.exists(agent_name):
        raise ValueError(
            f"agent {agent_name!r} not found; "
            f"create it with `reyn agent new {agent_name}`"
        )

    session = await _get_session(registry, agent_name, sid=sid)

    from reyn.runtime.message_bus import MessageBus  # noqa: PLC0415 — lazy import
    from reyn.runtime.transport import McpRef  # noqa: PLC0415 — lazy import

    chain_id = new_chain_id()
    req_id = f"mcp-{chain_id}"

    # Serialize concurrent calls to the same (agent, sid) session — the lock
    # keeps baseline → MessageBus.request → history-read atomic per session
    # (proposal 0067 P1, #3978: rekeyed from agent_name alone — this lock
    # protects THIS session's history, and an agent can have more than one
    # live session; see agent_locks.py's own docstring).
    async with _get_agent_lock(agent_name, sid):
        baseline_seq = _history_baseline_seq(session)
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
            override_channel_id = getattr(
                intervention_override, "channel_id", None,
            )
            if override_channel_id is not None:
                session.register_intervention_listener(override_channel_id)
        try:
            replies = await bus.request(
                session,
                kind=inbox_kind,
                payload={"text": message, "chain_id": chain_id},
                reply_to=McpRef(request_id=req_id),
                timeout=timeout,
            )
        finally:
            if intervention_override is not None:
                if override_channel_id is not None:
                    session.unregister_intervention_listener(override_channel_id)
        new_replies = _new_agent_history_entries(
            session, baseline_seq, chain_id=chain_id,
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
        "running_run_ids": [],
        "limit_stopped": limit_stopped,
    }


def _is_quiescent_after_bus(session) -> bool:
    """Check if the session is quiescent after MessageBus.request returns.

    MessageBus already waited for quiescence; this is a final check
    that captures the partial=True case (timeout fired before quiescence).
    """
    if not session.inbox.empty():
        return False
    return True


# ── SDK glue ────────────────────────────────────────────────────────────────


def build_server(
    registry: "AgentRegistry",
    *,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    extra_tools: "list[ExtraTool] | None" = None,
):
    """Construct an ``mcp.server.Server`` wired to the given registry.

    Imports of the ``mcp`` SDK are deferred so the module itself can be
    imported in test environments where ``mcp`` is not installed (the
    tests of this module install it via the ``mcp`` extra; the rest of
    the suite doesn't touch this surface).

    ``extra_tools`` are plugin-supplied tools (e.g. a gateway plugin's outbound
    send tool, #1805): each is exposed in ``list_tools`` and dispatched in
    ``call_tool`` after the built-in tools (built-ins take precedence on a name
    clash).

    #4368 (mcp 2.0 port, arc #4412): registration + ``ctx`` shape go through
    the ``_mcp_server_boundary.build_mcp_server`` seam — see that module's
    docstring for the full rationale (mirrors ``_fastmcp_boundary.py``'s
    #3698 P2 precedent). Handler bodies below are written in the seam's
    ``(ctx, params) -> Result`` shape (mcp 2.0's real handler signature —
    the seam adapts this onto the CURRENT pin's decorator API internally,
    and collapses to a near-passthrough once the pin bumps) and return the
    typed ``<X>Result`` object the seam expects (``ListToolsResult``/
    ``CallToolResult``/``ReadResourceResult``).

    Object CONSTRUCTION inside the handler bodies (``Tool(...)``,
    ``TextResourceContents(...)``, …) is a SEPARATE axis the seam does NOT
    cover (owner ruling via lead-coder, #4368: reyn adding a constructor
    function per SDK type would mean reyn's own surface grows every time
    the SDK's vocabulary grows — not reyn's responsibility, same
    discriminator as #4354's provider-layer ruling). Construction is
    written plain, in the CURRENTLY INSTALLED pin's own field-name
    vocabulary (``inputSchema``/``mimeType``/… — 1.x camelCase today); a
    pin-bump PR flips every such call site to 2.0's vocabulary in one
    mechanical pass, alongside this docstring.
    """
    from mcp.types import (
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        ReadResourceRequestParams,
        ReadResourceResult,
        TextContent,
        TextResourceContents,
        Tool,
    )

    from reyn.mcp._mcp_server_boundary import build_mcp_server

    _extra_tools = list(extra_tools or [])

    async def _list_tools(ctx: "object", params: "object") -> "ListToolsResult":  # type: ignore[no-redef]
        return ListToolsResult(tools=[
            Tool(
                name="list_agents",
                description=(
                    "List the agents registered in the current Reyn project. "
                    "Returns each agent's name and a short role excerpt."
                ),
                input_schema={
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
                    "how to respond; multi-turn conversation "
                    "accumulates because per-agent chat history persists."
                ),
                input_schema={
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
                input_schema={
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
            *[
                Tool(
                    name=et.name,
                    description=et.description,
                    input_schema=et.input_schema,
                )
                for et in _extra_tools
            ],
        ])

    async def _call_tool(  # type: ignore[no-redef]
        ctx: "object", params: "CallToolRequestParams",
    ) -> "CallToolResult":
        name = params.name
        arguments = params.arguments or {}
        if name == "list_agents":
            agents = await list_agents_impl(registry)
            import json
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(agents))])

        if name == "send_to_agent":
            agent_name = (arguments or {}).get("agent_name") or ""
            message = (arguments or {}).get("message") or ""
            if not agent_name:
                return CallToolResult(content=[TextContent(type="text", text="error: agent_name is required")])
            if not message:
                return CallToolResult(content=[TextContent(type="text", text="error: message is required")])

            # issue #271 M1: progress emit bridge — if the client provided
            # a progressToken in this request's metadata, subscribe a
            # bridge to the agent's audit_events EventLog that translates
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
                registry, agent_name, ctx,
            )
            # issue #270 Phase B: build MCP-side iv observer. When a
            # When a UserIntervention is emitted, this bus pushes the
            # iv payload to the peer via progress notification + lets
            # the peer answer via the ``answer_intervention`` tool.
            iv_bus = await _make_mcp_intervention_bus(
                registry, agent_name, ctx,
            )
            try:
                # FP-0043 S4b-6: run the invocation on the agent's SHARED mcp
                # session (isolated from "main"); resolve-or-spawn it (no run-loop
                # — driven inline by MessageBus.request). The single shared session
                # preserves the request-response continuity (state preserved on
                # the inbox for the next call).
                from reyn.runtime.mcp_routing import mcp_session_id, resolve_mcp_session
                resolve_mcp_session(registry, agent_name)
                result = await send_to_agent_impl(
                    registry,
                    agent_name=agent_name,
                    message=message,
                    timeout=timeout,
                    intervention_override=iv_bus,
                    sid=mcp_session_id(),
                )
            except (ValueError, FileNotFoundError) as e:
                return CallToolResult(content=[TextContent(type="text", text=f"error: {e}")])
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
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))])

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
                return CallToolResult(content=[TextContent(type="text", text="error: agent_name is required")])
            if not run_id:
                return CallToolResult(content=[TextContent(type="text", text="error: run_id is required")])
            if not registry.exists(agent_name):
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "answered": False,
                        "reason": f"agent {agent_name!r} not found",
                    }),
                )])
            try:
                # FP-0043 S4b-6: the run lives on the shared mcp session — resolve
                # it (not "main") so the pending iv is answered on the right session.
                from reyn.runtime.mcp_routing import resolve_mcp_session
                session = resolve_mcp_session(registry, agent_name)
            except Exception as exc:  # noqa: BLE001 — defensive
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "answered": False,
                        "reason": f"agent load failed: {exc}",
                    }),
                )])
            answer = InterventionAnswer(text=text, choice_id=choice_id)
            delivered = await session.answer_pending_intervention(run_id, answer)
            return CallToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "answered": bool(delivered),
                    "reason": (
                        None if delivered else
                        "already answered or no pending intervention"
                    ),
                }),
            )])

        # Plugin-supplied tools (#1805) — dispatched after the built-ins, so a
        # built-in name always wins on a clash.
        for et in _extra_tools:
            if name == et.name:
                result = await et.handler(arguments or {})
                return CallToolResult(content=[TextContent(type="text", text=result)])

        return CallToolResult(content=[TextContent(type="text", text=f"error: unknown tool {name!r}")])

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

    async def _read_resource(  # type: ignore[no-redef]
        ctx: "object", params: "ReadResourceRequestParams",
    ) -> "ReadResourceResult":
        """Resolve a ``reyn-tool-result://<agent>/<artifact>`` URI to its
        body for cross-protocol consumers (= external MCP clients).

        Returns a ``ReadResourceResult`` wrapping ``TextResourceContents``
        directly — the seam's ``_read_resource`` adapter (see
        ``_mcp_server_boundary.py``) unwraps this into the CURRENT pin's
        ``list[ReadResourceContents]`` convenience shape internally, so
        this handler body always returns the 2.0-shaped typed Result
        regardless of which pin is installed. Unsupported URI schemes and
        missing files surface as
        ``text/plain`` content with an ``error: ...`` body so the client
        still gets a structured response rather than a transport error.
        Path-traversal escapes propagate as PermissionError (= the MCP
        framework wraps as an error to the client).
        """
        from reyn.data.workspace.media_store import (
            MediaStore,
            MediaStoreConfig,
            parse_resource_uri,
        )
        uri_str = str(params.uri)
        parsed = parse_resource_uri(uri_str)
        if parsed is None:
            # Unsupported URI scheme (= not ``reyn-tool-result://...``).
            # External MCP clients sometimes pass other schemes (= file://,
            # https://); we explicitly only resolve our vendor scheme via
            # this handler. For https:// URLs, the client should fetch
            # directly (= the URL points at our own resources router).
            return ReadResourceResult(contents=[TextResourceContents(
                uri=uri_str,
                mime_type="text/plain",
                text=(
                    f"error: unsupported resource URI scheme: {uri_str!r}. "
                    "Reyn MCP server resolves reyn-tool-result://<agent>/<artifact> only; "
                    "for https:// URLs fetch directly via the resources router."
                ),
            )])
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
            return ReadResourceResult(contents=[TextResourceContents(
                uri=uri_str,
                mime_type="text/plain",
                text=(
                    f"error: tool result not found for URI {uri_str!r} "
                    "(= deleted by user, or never existed on this Reyn instance)"
                ),
            )])
        return ReadResourceResult(contents=[TextResourceContents(
            uri=uri_str, mime_type="text/plain", text=body,
        )])

    return build_mcp_server(
        "reyn",
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_read_resource=_read_resource,
    )


async def _make_mcp_intervention_bus(
    registry: "AgentRegistry",
    agent_name: str,
    ctx: "object",
) -> "_MCPInterventionBus | None":
    """Build an MCP iv-observer for the duration of one ``send_to_agent``
    call (issue #270 Phase B).

    issue #292 α extended to MCP: when a request handled via
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

    #4368 (mcp 2.0 port): *ctx* is the ``ServerRequestContext`` mcp 2.0's
    ``on_call_tool`` handler receives directly as its first argument —
    see :func:`_make_mcp_progress_bridge`'s docstring for the same note in
    full (no more ``server.request_context`` lookup, no more unavailable-
    context defensive catch — the runner never calls a registered handler
    without a context, so this function no longer has a ``None``-returning
    path for that reason; kept ``| None`` on the return type only because
    a future caller could still reasonably want that shape, not because
    this function itself produces it today).
    """
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
      - Does NOT await ``iv.future``. The handler awaits for the
        in-flight turn; the peer answers via the
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
    ctx: "object",
) -> "_MCPProgressBridge | None":
    """Build a progress-forwarding bridge for the duration of one
    ``send_to_agent`` call (issue #271 M1).

    Returns ``None`` when:
      - the client didn't set ``_meta.progress_token`` on this request
        (= peer doesn't care about progress, save the work)
      - the agent ``agent_name`` doesn't exist (= the caller's
        existence check in ``_call_tool`` will produce the standard
        error path; we silently no-op here)

    #4368 (mcp 2.0 port): *ctx* is the ``ServerRequestContext`` mcp 2.0's
    ``on_call_tool`` handler receives directly as its first argument — no
    ``server.request_context`` lookup needed any more (that was the
    lowlevel decorator API's only way to reach it; the constructor-kwarg
    handler shape gets it handed in for free, so the old
    ``try: ... except (LookupError, AttributeError)`` defensive catch
    around an unavailable context is now dead code, removed). ``ctx.meta``
    is a ``TypedDict`` on 2.0 (plain ``dict`` at runtime, NOT the 1.x
    line's pydantic ``BaseModel``) — attribute access
    (``ctx.meta.progressToken``) raises ``AttributeError``; ``.get(...)``
    on the renamed ``progress_token`` key is required. Confirmed live
    against a real ``mcp==2.0.0`` install, not assumed.

    The returned bridge has subscribed itself to the agent's chat
    audit-event log; callers MUST call ``bridge.detach()`` in a ``finally``
    to avoid the subscriber leaking across calls.
    """
    if ctx.meta is None or ctx.meta.get("progress_token") is None:
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
        progress_token=ctx.meta.get("progress_token"),
        related_request_id=ctx.request_id,
    )
    bridge.attach()
    return bridge


class _MCPProgressBridge:
    """Forwards selected chat audit-events of one agent to MCP progress notifications.

    issue #271 M1 (= M1-b lifecycle scope per owner decision); the forwarded
    selection and its wording are declared once in
    :data:`PROGRESS_LIFECYCLE_EVENTS` / :func:`format_progress_message`, shared
    verbatim with the A2A bridge (#3357).

    ``progress`` is monotonic (= ordinal counter) since we don't have a
    meaningful total. The MCP spec accepts ``total=None`` for
    indeterminate progress; clients render as raw value or spinner.

    The subscriber runs synchronously in the audit-event log's dispatcher; it
    schedules the actual ``send_progress_notification`` as an asyncio
    task so we don't block the emitter on the MCP transport. Any
    transport / cancellation error in the background task is swallowed
    (= progress is best-effort; the main call must never fail because
    notification delivery failed).
    """

    TRACKED_EVENTS = PROGRESS_LIFECYCLE_EVENTS

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
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def detached(self) -> bool:
        """Read-only accessor for the bridge's detached flag.

        Symmetric with ``_A2AProgressBridge.detached``; tests verify the
        attach / detach lifecycle via this surface.
        """
        return self._detached

    @property
    def tracked_task_count(self) -> int:
        """Read-only accessor for the number of in-flight notification tasks.

        Symmetric with ``_A2AProgressBridge.tracked_task_count``. Each
        scheduled task discards itself from ``_tasks`` on completion
        (#3390), so this stays bounded by concurrency, not by how many
        audit-events the bridge has ever forwarded.
        """
        return len(self._tasks)

    def attach(self) -> None:
        events = getattr(self._session, "_audit_events", None)
        if events is not None:
            # #5260: declare the fixed interest at registration instead of
            # letting every OTHER event reach _on_event just to be filtered
            # at its own top (the ``event_type not in self.TRACKED_EVENTS``
            # check there — kept as a defensive no-op, not removed, since a
            # narrowed declaration must never be the ONLY thing enforcing
            # the filter).
            events.add_subscriber(self._on_event, kinds=self.TRACKED_EVENTS)

    def detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        events = getattr(self._session, "_audit_events", None)
        if events is not None:
            events.remove_subscriber(self._on_event)
        # Best-effort: cancel in-flight notification tasks so they don't
        # outlive the request. Snapshot before cancelling: a done callback
        # discards its task from ``_tasks`` (#3390) via call_soon — never
        # synchronously inside this loop — so iterating the live set would
        # already be safe, but iterating a copy keeps this loop correct
        # even if a future change made completion synchronous.
        for task in list(self._tasks):
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
        message = format_progress_message(event_type, data)
        self._ordinal += 1
        ordinal = float(self._ordinal)
        try:
            task = asyncio.ensure_future(self._send(ordinal, message))
        except RuntimeError:
            # No running loop (= EventLog dispatched outside async context).
            # Skip — caller will see this event later if/when an async
            # context picks up the next event.
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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


def build_init_options(server: Any) -> Any:
    """Build the MCP ``initialize`` response options this server advertises.

    issue #271 M3: capability advertising. Declares what this server actually
    emits + handles so MCP clients can negotiate features before issuing
    ``send_to_agent`` calls. Reality must match the claim (= avoid the #267 Z-b
    "capability claim vs reality" mismatch pattern by deriving each entry from a
    concrete production wire):

      - ``NotificationOptions``: tools/prompts/resources lists are STATIC
        (= ``_list_tools`` returns the same tools every call, no
        ``notify_*_changed`` call sites in this module).
      - experimental ``reyn.progress.skill_lifecycle``: ``_MCPProgressBridge``
        subscribes the agent's chat audit-event log and emits
        ``notifications/progress`` for each kind in
        :data:`PROGRESS_LIFECYCLE_EVENTS` during ``send_to_agent``. The
        advertised ``events`` list is DERIVED from that constant rather than
        restated, so the declaration cannot drift from the filter (#3357 — it
        previously named two kinds no producer emitted). The capability KEY is
        a legacy artifact from the skill-based era, kept as-is because it is a
        published wire-protocol string clients match on.
      - experimental ``reyn.cancellation.cooperative``: ``notifications/cancelled``
        propagation through ``asyncio.CancelledError`` → in-flight agent turn
        interruption.

    Separated from :func:`serve_stdio` so the advertisement is assertable
    without standing up a stdio transport.
    """
    from mcp.server import NotificationOptions

    return server.create_initialization_options(
        notification_options=NotificationOptions(
            prompts_changed=False,
            resources_changed=False,
            tools_changed=False,
        ),
        experimental_capabilities={
            "reyn.progress.skill_lifecycle": {
                "version": 1,
                "events": sorted(PROGRESS_LIFECYCLE_EVENTS),
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

    # No extra_tools here: the stdio MCP server hosts no gateway outbound tools
    # (#1805) — those are reyn-web-scoped (webhook plugins mount in the FastAPI
    # app + register_tools is collected onto app.state). A stdio CLI has no app,
    # so there is nothing to host. The SSE path (web/routers/mcp.py) passes
    # extra_tools; this asymmetry is by design, not an oversight.
    server = build_server(registry, timeout=timeout)
    init_options = build_init_options(server)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        try:
            await registry.shutdown()
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("registry shutdown after MCP serve: %s", e)


__all__ = [
    "build_init_options",
    "build_server",
    "list_agents_impl",
    "send_to_agent_impl",
    "serve_stdio",
    "DEFAULT_SEND_TIMEOUT_SECONDS",
]
