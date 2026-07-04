"""Programmatic session-spawn + run+collect entry points for non-LLM callers.

``AgentRegistry.spawn_session_recorded`` is the clean action-layer seam behind
``session_spawn`` (the LLM tool): it spawns a fresh-context session, persists +
enforces any capability narrowing, and emits the rewind-tracked
``session_spawned`` WAL event. The LLM tool path reaches it only through
``RouterCallerState.spawn_session_fn``, a closure the router loop builds — so a
deterministic, non-LLM caller (e.g. a Pipeline executor's ``agent`` step) has no
router-free way in.

``spawn_ephemeral_session`` closes that gap: it calls the SAME
``spawn_session_recorded`` primitive directly, with no ``RouterLoopHost`` /
``RouterCallerState`` / router-loop involvement at all — just a registry and a
target identity. It hardcodes ``mode="ephemeral"`` (the only mode a
programmatic driver needs today) and returns the new session id. Turn/token
budgeting for spawned sessions is a separate, harder mechanism (per-session
``max_turns``) and is deliberately out of scope here.

``run_agent_step`` (R5: agent-step run+collect,
``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``) composes THREE
existing primitives — it adds no new session/LLM machinery of its own:

  1. ``spawn_ephemeral_session`` (above) — spawn the leaf worker, with a
     narrowing that STRUCTURALLY denies delegation (see
     ``_build_agent_step_narrowing``): an ``agent`` step must not itself
     delegate mid-turn, because ``MessageBus.request``'s quiescence
     predicate only checks ``inbox.empty()`` — a mid-turn ``delegate_to_agent``
     would make it return early on a pending chain the spawned session is
     still awaiting a reply for.
  2. ``MessageBus.request`` (``runtime/message_bus.py``) — the existing
     synchronous run+collect: put a ``user`` message on the spawned
     session's inbox, pump ``run_one_iteration`` on the caller's own task
     until quiescent, and return every ``OutboxMessage`` emitted during the
     turn. The ephemeral session self-vanishes via
     ``_maybe_schedule_ephemeral_vanish`` once the turn leaves it quiescent
     with no pending chains — no explicit close needed here.
  3. ``core.pipeline.schema.validate`` — when the caller declares a
     ``schema``, the joined ``kind="agent"`` reply text is JSON-parsed
     defensively and validated post-hoc (exactly the executor's
     ``ToolStep`` pattern — there is no schema-constrained *generation* in
     the router path today).
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from reyn.runtime.errors import AgentStepError
from reyn.runtime.transport import SystemRef

if TYPE_CHECKING:
    from reyn.core.pipeline.schema import SchemaRegistry
    from reyn.runtime.registry import AgentRegistry

# Tool names an ``agent`` pipeline step must never reach — a leaf worker (R6
# session-hierarchy constraint 4: "E_i are spawn-tree LEAVES"). Two distinct
# reasons collapse into one deny-set:
#   - ``delegate_to_agent``: a mid-turn delegation would make
#     ``MessageBus.request``'s quiescence predicate (inbox.empty()) return
#     early on a pending chain the spawned session is still awaiting a reply
#     for (see the module docstring).
#   - ``run_pipeline`` (IS-1, R6 S3): nesting a pipeline launch inside an
#     ``agent`` step would let a step spawn ANOTHER pipeline at runtime,
#     defeating the transitive-closure cost-bound approval a REGISTERED
#     pipeline gets at ``run_pipeline`` call time — nesting is ``call``-only.
# ``_expand_tool_forms`` (capability_profile.py) derives every invocable alias
# (bare + qualified) from each name here, so listing the bare tool name is
# sufficient — the qualified catalog form (``multi_agent__delegate`` /
# ``pipeline__run``) is covered too.
_DELEGATION_DENY_TOOLS: tuple[str, ...] = ("delegate_to_agent", "run_pipeline")

# MessageBus.request has no default — an agent step needs one so callers
# aren't forced to pick a number for the common case.
_DEFAULT_AGENT_STEP_TIMEOUT_S: float = 120.0


async def spawn_ephemeral_session(
    registry: "AgentRegistry", *, identity: str, narrowing: "dict | None" = None,
) -> str:
    """Spawn an ephemeral session under ``identity`` for a non-LLM caller.

    Thin, direct wrapper over ``registry.spawn_session_recorded(identity,
    mode="ephemeral", narrowing=narrowing)`` — the same call the
    ``session_spawn`` tool's handler reaches via ``spawn_session_fn``, so the
    emitted ``session_spawned`` WAL event + the spawned session's narrowing
    enforcement are byte-identical to the tool path. Returns the new session id
    (the ``session_spawned`` event's ``sid``).

    No task is submitted here — that stays the caller's job (the Pipeline
    executor's ``agent`` step, in the eventual wiring), same as the S1bc
    action-layer seam does not submit either."""
    return await registry.spawn_session_recorded(
        identity, mode="ephemeral", narrowing=narrowing,
    )


def _build_agent_step_narrowing(capabilities: "list[str] | None") -> dict:
    """The per-session narrowing an ``agent`` step spawns under.

    ``tool_deny`` always includes ``_DELEGATION_DENY_TOOLS`` — a v1
    structural constraint (R5), not something the caller's ``capabilities``
    can re-open: ``capability_profile`` resolution is deny-always-wins
    (``profile_permits``: ``in_allow and tool not in tool_deny``), so even a
    ``capabilities`` list that names a delegation tool is denied at the live
    gate. ``tool_allow`` is set only when the caller passes an explicit
    ``capabilities`` list — omitting it (``None``) leaves the agent's normal
    envelope untouched (restrict-only narrowing, never a re-grant)."""
    narrowing: dict[str, Any] = {"tool_deny": list(_DELEGATION_DENY_TOOLS)}
    if capabilities is not None:
        narrowing["tool_allow"] = list(capabilities)
    return narrowing


async def run_agent_step(
    registry: "AgentRegistry",
    *,
    identity: str,
    prompt: str,
    capabilities: "list[str] | None" = None,
    schema: "str | None" = None,
    schema_registry: "SchemaRegistry | None" = None,
    chain_id: "str | None" = None,
    timeout: "float | None" = None,
) -> Any:
    """Spawn an ephemeral session, run one turn, collect + return its output.

    The future Pipeline executor's ``agent`` step primitive (R5): spawn a
    leaf-worker session under ``identity`` (capability-narrowed to
    ``capabilities`` plus a structural delegation deny, see
    ``_build_agent_step_narrowing``), feed it ``prompt`` as a single ``user``
    turn via ``MessageBus.request``, and return its collected reply.

    With ``schema`` unset, returns the joined ``kind="agent"`` reply text
    verbatim. With ``schema`` set (a name registered in ``schema_registry``),
    the text is JSON-parsed and validated against it; the parsed + validated
    value is returned. A ``schema`` without a ``schema_registry``, non-JSON
    text, or a schema-non-conforming value each raise ``AgentStepError`` — a
    normal step failure for the executor's retry/error path, not a
    construction-time error.

    ``chain_id`` defaults to a fresh uuid4 hex (mirrors ``MessageBus``'s own
    ``_new_request_id``). ``timeout`` defaults to
    ``_DEFAULT_AGENT_STEP_TIMEOUT_S`` seconds.
    """
    from reyn.core.pipeline.schema import validate
    from reyn.runtime.message_bus import MessageBus

    narrowing = _build_agent_step_narrowing(capabilities)
    sid = await spawn_ephemeral_session(registry, identity=identity, narrowing=narrowing)
    session = registry.get_session(identity, sid)
    if session is None:
        raise AgentStepError(
            f"run_agent_step: spawn_ephemeral_session({identity!r}) returned "
            f"sid={sid!r}, but registry.get_session({identity!r}, {sid!r}) "
            "found no live session — the registry's session_factory may not "
            "register the spawned session under its own name/sid."
        )

    bus = MessageBus()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": prompt, "chain_id": chain_id or uuid.uuid4().hex},
        reply_to=SystemRef(),
        timeout=timeout if timeout is not None else _DEFAULT_AGENT_STEP_TIMEOUT_S,
    )
    text = "\n\n".join(r.text for r in replies if r.kind == "agent")

    if schema is None:
        return text

    if schema_registry is None:
        raise AgentStepError(
            f"run_agent_step(schema={schema!r}) requires schema_registry "
            "(no registry to validate against)."
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentStepError(
            f"run_agent_step(schema={schema!r}): agent step output is not "
            f"valid JSON: {exc}. Output: {text!r}"
        ) from exc
    result = validate(parsed, schema, schema_registry)
    if not result.conforming:
        details = "; ".join(f"{e.path or '<root>'}: {e.message}" for e in result.errors)
        raise AgentStepError(
            f"run_agent_step(schema={schema!r}): agent step output does not "
            f"conform to schema: {details}"
        )
    return parsed
