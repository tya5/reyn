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

``start_pipeline_run`` (IS-2) is the ASYNC counterpart of the sync
``run_pipeline`` tool: it spawns a dedicated pipeline driver-session (born
with its work-order — the D案 architecture), persists ``invocation.json``
before step 0 can run, swaps in the ``PipelineExecutorDriver`` and nudges the
run — returning the ``run_id`` immediately while the result arrives later as
a ``pipeline_result`` inbox message. See its docstring for the crash-safety
ordering.
"""
from __future__ import annotations

import json
import re
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
#   - ``run_pipeline`` / ``run_pipeline_async`` (IS-1/IS-2, R6 S3): nesting a
#     pipeline launch inside an ``agent`` step would let a step spawn ANOTHER
#     pipeline at runtime, defeating the transitive-closure cost-bound approval
#     a REGISTERED pipeline gets at launch time — nesting is ``call``-only.
#     The async launch is the same escape hatch as the sync one (sibling).
# ``_expand_tool_forms`` (capability_profile.py) derives every invocable alias
# (bare + qualified) from each name here, so listing the bare tool name is
# sufficient — the qualified catalog form (``multi_agent__delegate`` /
# ``pipeline__run``) is covered too.
_DELEGATION_DENY_TOOLS: tuple[str, ...] = (
    "delegate_to_agent", "run_pipeline", "run_pipeline_async",
)

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


async def start_pipeline_run(
    registry: "AgentRegistry",
    *,
    pipeline: "object",
    pipeline_name: str,
    input: "dict | None",
    reply_to_agent: str,
    reply_to_sid: str,
    state_log: "object",
    run_id: "str | None" = None,
) -> str:
    """IS-2: launch an async pipeline run in a dedicated driver-session (D案).

    The launch sequence, in crash-safety order:

      1. spawn the driver-session under the INVOKER's identity
         (``spawn_session_recorded(mode="persistent")`` — the same recorded
         seam as every other programmatic spawn; persistent because the
         session must survive a crash to be re-woken, and its reclamation is
         the driver's own terminal ephemeral-vanish, not inbox-idleness).
         Same identity ⇒ the driver's permission envelope is the invoker's
         (⊆ by construction).
      2. persist the work-order (``invocation.json`` — full serialized
         pipeline + input + reply address + the driver's own (agent, sid) +
         the WAL seq at spawn) BEFORE step 0 can possibly run. From this
         point the run is crash-recoverable: ``AgentRegistry.restore_all``'s
         pipeline scan re-creates + re-wakes the driver-session from this
         file alone.
      3. swap in the :class:`~reyn.runtime.services.pipeline_executor_driver.
         PipelineExecutorDriver` (``Session.set_loop_driver``) and nudge the
         run-loop with an empty user turn — the D案 "run/resume" nudge whose
         text carries no meaning — then boot the detached run-loop pump
         (``ensure_session_running``; no forwarder — a driver-session has no
         user-facing output).

    Returns the ``run_id`` immediately; the result arrives later on the
    invoker's inbox as a ``pipeline_result`` message."""
    from reyn.core.events.config_recovery import reyn_root
    from reyn.core.pipeline.serde import pipeline_to_dict
    from reyn.core.pipeline.work_order import (
        PipelineWorkOrder,
        pipeline_run_dir,
        write_invocation,
    )
    from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver

    root = reyn_root(state_log.path)
    if root is None:
        raise ValueError(
            "start_pipeline_run requires a .reyn-anchored StateLog (the "
            f"work-order/recovery files live under it); got {state_log.path!r}"
        )
    # The run_id becomes a directory segment (.reyn/pipeline/state/<run_id>/),
    # so the embedded pipeline name is sanitized to one safe path component.
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", pipeline_name) or "pipeline"
    rid = run_id or f"pipeline-{safe_name}-{uuid.uuid4().hex}"
    sid = await registry.spawn_session_recorded(reply_to_agent, mode="persistent")
    work_order = PipelineWorkOrder(
        run_id=rid,
        pipeline_name=pipeline_name,
        pipeline=pipeline_to_dict(pipeline),
        input=dict(input) if input else None,
        reply_to_agent=reply_to_agent,
        reply_to_sid=reply_to_sid,
        driver_agent=reply_to_agent,
        driver_sid=sid,
        spawn_seq=state_log.current_seq,
    )
    write_invocation(pipeline_run_dir(root, rid), work_order)
    session = registry.get_session(reply_to_agent, sid)
    if session is None:
        raise RuntimeError(
            f"start_pipeline_run: spawned driver-session ({reply_to_agent!r}, "
            f"{sid!r}) not found in the registry"
        )
    session.set_loop_driver(
        PipelineExecutorDriver(work_order, registry=registry, state_log=state_log)
    )
    await session.submit_user_text("")  # the no-payload run nudge (D案)
    registry.ensure_session_running(reply_to_agent, sid)
    return rid
