"""MCP server — expose Reyn agents to outer LLM clients.

This is the *server* side counterpart to :mod:`reyn.mcp_client` (which
*consumes* third-party MCP servers). External clients (Claude Code,
Cursor, OpenAI Agents SDK with MCP enabled, …) spawn ``reyn mcp serve``
as a stdio subprocess and converse with a Reyn agent through two tools:

  - ``reyn:list_agents()`` — enumerate registered agents.
  - ``reyn:send_to_agent(agent_name, message)`` — submit one user
    message to a named agent and block (with timeout) for the final
    reply text.

Multi-turn continuity falls out for free: ``ChatSession.history`` is
persistent across calls because the registry caches each session
in-process and ``ChatSession.load_history`` rehydrates from
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

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry


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


# Per-agent serialization lock. Concurrent FastAPI request handlers (e.g.
# the A2A endpoint) can reach ``send_to_agent_impl`` in parallel for the
# same agent; without serialization both coroutines race on
# ``session.history[-1]`` (RouterLoop reads the wrong prompt) and on
# reply harvesting (each picks up the other's ``role="agent"`` entries).
# FP-0013: with MessageBus, the inbox is the serialization point but the
# lock is retained as a belt-and-suspenders measure during the migration
# period — it prevents concurrent calls from racing on history harvest
# (baseline → MessageBus.request → history-read must be atomic per agent).
_AGENT_LOCKS: dict[str, asyncio.Lock] = {}


def _get_agent_lock(agent_name: str) -> asyncio.Lock:
    return _AGENT_LOCKS.setdefault(agent_name, asyncio.Lock())


async def _get_session(registry: "AgentRegistry", name: str) -> "object":
    """Return a loaded ChatSession for `name`.

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
        if msg.role != "agent" or not msg.text:
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
        has_reply = any(
            msg.role == "agent" for msg in session.history[baseline:]
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
        replies = await bus.request(
            session,
            kind="user",
            payload={"text": message, "chain_id": chain_id},
            reply_to=McpRef(request_id=req_id),
            timeout=timeout,
        )
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

    return {
        "reply": reply_text,
        "partial": (not idle),
        "agent": agent_name,
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
            try:
                result = await send_to_agent_impl(
                    registry,
                    agent_name=agent_name,
                    message=message,
                    timeout=timeout,
                )
            except ValueError as e:
                return [TextContent(type="text", text=f"error: {e}")]
            import json
            return [TextContent(type="text", text=json.dumps(result))]

        return [TextContent(type="text", text=f"error: unknown tool {name!r}")]

    return server


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
    init_options = server.create_initialization_options()
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
