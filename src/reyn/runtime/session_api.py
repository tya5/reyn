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

``start_pipeline_run`` (IS-2) and ``run_pipeline_attached`` (IS-6) are the two
launch paths onto the SAME pipeline driver-session (the D案 architecture — a
session born with its work-order, ``invocation.json`` persisted before step 0,
a ``PipelineExecutorDriver`` swapped in), sharing the ``_spawn_pipeline_driver_session``
prefix and differing only in how the caller drives + collects:

  - ``start_pipeline_run`` (ASYNC) nudges the run and boots a DETACHED pump
    (``ensure_session_running``), returning ``run_id`` immediately; the result
    arrives later as a ``pipeline_result`` inbox message (``notify_reply=True``).
  - ``run_pipeline_attached`` (SYNC) drives the driver-session INLINE on the
    caller's own task via ``MessageBus.request`` (the same run+collect primitive
    ``run_agent_step`` uses), so the caller blocks, sees live ``pipeline_step_*``
    events on the driver-session's ``EventLog``, and collects the terminal marker
    in-band via ``read_result`` (``notify_reply=False`` — no redundant reply
    turn). "Sync = async + an attached live view": because it is the SAME
    driver-session, a crash mid-attach is auto-resumed by the existing recovery
    scan (which re-creates the driver with ``notify_reply=True`` → the result
    degrades to inbox delivery), so sync pipelines are crash-recoverable too.
    Optional ``tool``/``caller_events`` params (#2570, the TUI bridge) let it
    also emit a ``pipeline_run_attached`` marker onto the CALLER's own
    ``EventLog`` (see the function docstring) — the driver-session's live
    events are on a DIFFERENT EventLog than the one the human-attached caller
    (the TUI) watches, so this marker is the signal that bridges the two.
    #2708 P3.1: a ``present`` step's OUTPUT (Half-A) reaches the parent chat surface
    BY CONSTRUCTION — the attached driver-session is spawned with a
    ``SpawnBridgePresentationConsumer`` (``runtime/presentation_consumer.py``) bound to the
    PARENT session, so its present sink IS the parent's sink (structurally replacing the
    #2707 interim outbox forward, which is removed — keeping both would double-deliver). Its
    AUDIT event (Half-B) is bridged separately: ``presented`` is a driver-EventLog P6 event
    (not a ``"presentation"`` outbox message), so it rides the same #2570 driver→parent
    EventLog bridge as ``pipeline_step_*`` — extended (``lifecycle_forwarder``) to re-emit
    ``presented`` onto the PARENT's log with ``bridged_from=<driver_sid>`` provenance.
    Together: both the visible present and its audit trail reach the parent, closing the
    driver-isolation split. (Detached/async present is out of P3.1 scope — no attached
    parent surface exists; tracked as the P3 spawn-routing completeness gate.)
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
#   - ``run_pipeline`` / ``run_pipeline_async`` / ``run_pipeline_inline`` /
#     ``run_pipeline_inline_async`` (IS-1/IS-2/IS-4, R6 S3): nesting a pipeline
#     launch inside an ``agent`` step would let a step spawn ANOTHER pipeline at
#     runtime, defeating the transitive-closure cost-bound approval a pipeline
#     gets at launch time — nesting is ``call``-only. The async + inline launch
#     verbs are the same escape hatch as the sync registered one (siblings); the
#     inline verbs get NO exemption (an ad-hoc pipeline is still non-grantable
#     inside a pipeline). Kept in lock-step with ``pipeline_verbs.
#     _PIPELINE_STEP_DENY_TOOLS`` (the tool-step sibling of this agent-step deny).
# ``_expand_tool_forms`` (capability_profile.py) derives every invocable alias
# (bare + qualified) from each name here, so listing the bare tool name is
# sufficient — the qualified catalog form (``multi_agent__delegate`` /
# ``pipeline__run``) is covered too.
_DELEGATION_DENY_TOOLS: tuple[str, ...] = (
    "delegate_to_agent", "run_pipeline", "run_pipeline_async",
    "run_pipeline_inline", "run_pipeline_inline_async",
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


async def _spawn_pipeline_driver_session(
    registry: "AgentRegistry",
    *,
    pipeline: "object",
    pipeline_name: str,
    input: "dict | None",
    reply_to_agent: str,
    reply_to_sid: str,
    state_log: "object",
    notify_reply: bool,
    run_id: "str | None" = None,
    schema_registry: "SchemaRegistry | None" = None,
    attached_parent_session: "Any | None" = None,
) -> "tuple[Any, str, str]":
    """Spawn + arm a pipeline driver-session, up to (but NOT including) the
    run/resume nudge — the shared launch prefix of the async (``start_pipeline_run``)
    and sync-attached (``run_pipeline_attached``) paths.

    In crash-safety order:

      1. spawn the driver-session under the INVOKER's identity
         (``spawn_session_recorded(mode="persistent")`` — the same recorded seam
         as every other programmatic spawn; persistent because the session must
         survive a crash to be re-woken). Same identity ⇒ the driver's
         permission envelope is the invoker's (⊆ by construction).
      2. persist the work-order (``invocation.json`` — full serialized pipeline +
         input + reply address + the driver's own (agent, sid) + the WAL seq at
         spawn + (#2572) ``schema_defs``, the launch's ``schema_registry``
         serialized via ``SchemaRegistry.as_dict()``) BEFORE step 0 can possibly
         run. From this point the run is crash-recoverable: the recovery scan
         re-creates + re-wakes the driver-session from this file alone (with
         ``notify_reply=True`` — the originally-attached caller is gone after a
         crash), and ``PipelineExecutorDriver.run_turn`` rebuilds the registry
         from ``schema_defs`` on every wake — so a ``verify: schema`` step is
         enforced on the original run and on a re-created driver-session alike.
      3. swap in the :class:`~reyn.runtime.services.pipeline_executor_driver.
         PipelineExecutorDriver` (``Session.set_loop_driver``), carrying the
         runtime ``notify_reply`` — True for the async fire-and-forget path
         (the caller awaits the inbox), False for the sync attached path (the
         caller collects the result in-band via ``read_result``).

    Returns ``(driver_session, run_id, driver_sid)``; the caller drives the run
    (nudge + detached pump, or attached ``MessageBus.request``).

    #2708 P3.1 (Half-A, visible output): when ``attached_parent_session`` is given (the
    ATTACHED path only — ``run_pipeline_attached`` passes the live caller session), the
    driver-session is spawned with a ``SpawnBridgePresentationConsumer`` bound to that
    parent, so a ``present`` step's render reaches the PARENT's surface by construction
    (structurally replacing the #2707 interim outbox forward). The DETACHED path
    (``start_pipeline_run``) passes ``None`` — there is no live attached surface, so the
    driver keeps the default self-bound consumer (its present is audit-only in its own
    log, correct with no attached parent)."""
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
            "pipeline launch requires a .reyn-anchored StateLog (the "
            f"work-order/recovery files live under it); got {state_log.path!r}"
        )
    # The run_id becomes a directory segment (.reyn/pipeline/state/<run_id>/),
    # so the embedded pipeline name is sanitized to one safe path component.
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", pipeline_name) or "pipeline"
    rid = run_id or f"pipeline-{safe_name}-{uuid.uuid4().hex}"
    # #2708 P3.1 Half-A: on the attached path, bind the driver's present sink to the
    # PARENT's consumer so its render reaches the parent surface by construction. Detached
    # spawns pass None → the default self-bound outbox consumer (byte-identical).
    bridge_consumer: "Any | None" = None
    if attached_parent_session is not None:
        from reyn.runtime.presentation_consumer import SpawnBridgePresentationConsumer

        bridge_consumer = SpawnBridgePresentationConsumer(
            parent_consumer=attached_parent_session.presentation_consumer,
            parent_session=attached_parent_session,
        )
    sid = await registry.spawn_session_recorded(
        reply_to_agent, mode="persistent", presentation_consumer=bridge_consumer,
    )
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
        schema_defs=schema_registry.as_dict() if schema_registry is not None else None,
    )
    write_invocation(pipeline_run_dir(root, rid), work_order)
    session = registry.get_session(reply_to_agent, sid)
    if session is None:
        raise RuntimeError(
            f"pipeline launch: spawned driver-session ({reply_to_agent!r}, "
            f"{sid!r}) not found in the registry"
        )
    session.set_loop_driver(
        PipelineExecutorDriver(
            work_order, registry=registry, state_log=state_log,
            notify_reply=notify_reply,
        )
    )
    return session, rid, sid


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
    schema_registry: "SchemaRegistry | None" = None,
) -> str:
    """IS-2: launch an ASYNC pipeline run in a dedicated driver-session (D案).

    Spawns + arms the driver-session (``_spawn_pipeline_driver_session`` with
    ``notify_reply=True`` — the caller got ``{started}`` and awaits the inbox),
    nudges the run-loop with an empty user turn (the D案 "run/resume" nudge whose
    text carries no meaning), then boots the DETACHED run-loop pump
    (``ensure_session_running``; no forwarder — a driver-session has no
    user-facing output).

    ``schema_registry`` (#2572), when given, is persisted onto the work-order
    (``schema_defs``) so the driver-session's ``verify: schema`` steps are
    enforced — on the original run and on any later crash-recovery re-wake.

    Returns the ``run_id`` immediately; the result arrives later on the invoker's
    inbox as a ``pipeline_result`` message."""
    session, rid, sid = await _spawn_pipeline_driver_session(
        registry,
        pipeline=pipeline,
        pipeline_name=pipeline_name,
        input=input,
        reply_to_agent=reply_to_agent,
        reply_to_sid=reply_to_sid,
        state_log=state_log,
        notify_reply=True,
        run_id=run_id,
        schema_registry=schema_registry,
    )
    await session.submit_user_text("")  # the no-payload run nudge (D案)
    registry.ensure_session_running(reply_to_agent, sid)
    return rid


async def run_pipeline_attached(
    registry: "AgentRegistry",
    *,
    pipeline: "object",
    pipeline_name: str,
    input: "dict | None",
    reply_to_agent: str,
    reply_to_sid: str,
    state_log: "object",
    timeout: "float | None" = None,
    run_id: "str | None" = None,
    tool: "str | None" = None,
    caller_events: "Any | None" = None,
    schema_registry: "SchemaRegistry | None" = None,
) -> dict:
    """IS-6: launch a SYNC pipeline run in a driver-session the caller ATTACHES to.

    "Sync = async + an attached live view": the SAME driver-session as
    ``start_pipeline_run`` (so a crash mid-run is auto-resumed by the existing
    recovery scan — sync pipelines are crash-recoverable, not a regression), but
    instead of a detached pump the caller drives the driver-session INLINE on its
    own task via ``MessageBus.request`` — the same run+collect primitive
    ``run_agent_step`` uses. The driver runs the whole pipeline to terminal in one
    nudge, emitting ``pipeline_step_*`` events to its own ``EventLog`` as it goes
    (a concurrent subscriber sees live progress), then the caller reads the
    terminal marker in-band via ``read_result`` (``notify_reply=False`` — no
    redundant ``pipeline_result`` turn to the caller's own session).

    Reply address = the INVOKING caller's own (agent, sid): on the attached happy
    path it is unused (delivery suppressed), but if the process CRASHES mid-attach
    the driver is destroyed and the recovery scan re-creates it with
    ``notify_reply=True`` → the result then degrades to async inbox delivery to
    this same caller. One reply address serves both paths; no new plumbing.

    **Cancel bridge (#2588)**: because the caller drives the driver-session inline
    via ``MessageBus.request`` (which is cancel-agnostic — it only pumps to
    quiescence), a Ctrl-C reaching ``cancel_inflight`` on the CALLER session would
    otherwise only cancel the caller's own turn-driver, never the spawned
    driver-session's ``PipelineExecutorDriver`` whose ``cancel_check`` the
    executor polls at each step boundary. Since the reply address IS the caller's
    own (agent, sid), it resolves (via ``registry.get_session``) to the SAME live
    Session instance whose ``cancel_inflight`` the Ctrl-C fires; this registers
    the driver's ``request_cancel`` as a cancel-forward on that caller session for
    the DURATION of the attached pump (unregistered in a ``finally`` so it never
    leaks past the run). A Ctrl-C then stops the pipeline at the next step
    boundary with a terminal ``cancelled`` marker, which ``read_result`` below
    returns to the caller as ``status="cancelled"``. Best-effort: an unresolvable
    caller session (should not happen on the attached path) skips the bridge.

    **TUI bridge marker (#2570)**: the driver-session's ``pipeline_step_*``
    events land on the DRIVER's own ``EventLog`` — a session distinct from the
    human-attached caller, which the TUI has no signal to bridge-subscribe to.
    When ``caller_events`` (an ``EventLog``) is given, right after the driver-
    session is spawned this emits a ``pipeline_run_attached`` marker onto it —
    ``{kind: "pipeline_run_attached", tool, run_id, driver_sid, agent_name,
    pipeline_name}`` — so a live view (the TUI) watching the CALLER's own
    EventLog learns the driver_sid to bridge-subscribe to for the run's
    duration (unsubscribing on the matching ``tool_call_completed``). ``tool``
    is the caller-supplied invoking tool name (``run_pipeline`` /
    ``run_pipeline_inline``) — this helper is shared by both, so it never
    hardcodes one. None (the default) skips the emit — used by callers with no
    attached live viewer to bridge to. Sync-attached-only: the async path
    (``start_pipeline_run``) has no attached caller and never emits this.

    Returns a ``dict``:
      - terminal reached → ``{"status": <ok|failed|cancelled>, "run_id", "output",
        "named_stores", "error"}`` from the marker (the caller shapes its tool
        result from this).
      - ``timeout`` elapsed with the pump still non-terminal → the run is NOT
        lost: the driver is flipped to ``notify_reply=True`` and handed to the
        detached pump (``ensure_session_running``), so it finishes and delivers to
        the caller's inbox later; returns ``{"status": "running_async", "run_id"}``.
        NOTE: with the D案 single-nudge driver a step runs to completion inside one
        non-preemptible ``run_one_iteration``, so ``timeout`` bounds the
        quiescence-polling loop, not a step already in flight — it is a safety net
        against a pump that returns non-terminal, not a mid-step wall-clock kill.

    ``schema_registry`` (#2572), when given, is persisted onto the work-order
    (``schema_defs``) so the driver-session's ``verify: schema`` steps are
    enforced — on the original run and on any later crash-recovery re-wake."""
    from reyn.core.events.config_recovery import reyn_root
    from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
    from reyn.runtime.message_bus import MessageBus

    # #2708 P3.1 Half-A: resolve the live caller (parent) session BEFORE the spawn so the
    # driver inherits its present sink by construction (the ``SpawnBridgePresentationConsumer``
    # is built inside ``_spawn_pipeline_driver_session`` from this parent). This is the SAME
    # (agent, sid) live Session the #2588 cancel-bridge resolves below — resolve it once and
    # reuse. None (should not happen on the attached path — the caller is live) → no bridge:
    # the driver keeps its default self-bound consumer (degrades to pre-fix isolation, never
    # blocks the run).
    caller_session = registry.get_session(reply_to_agent, reply_to_sid)

    session, rid, sid = await _spawn_pipeline_driver_session(
        registry,
        pipeline=pipeline,
        pipeline_name=pipeline_name,
        input=input,
        reply_to_agent=reply_to_agent,
        reply_to_sid=reply_to_sid,
        state_log=state_log,
        notify_reply=False,
        run_id=run_id,
        schema_registry=schema_registry,
        attached_parent_session=caller_session,
    )
    if caller_events is not None:
        caller_events.emit(
            "pipeline_run_attached",
            tool=tool, run_id=rid, driver_sid=sid,
            agent_name=reply_to_agent, pipeline_name=pipeline_name,
        )
    run_dir = pipeline_run_dir(reyn_root(state_log.path), rid)

    # #2588: bridge the attached caller's Ctrl-C to the DRIVER-session. The
    # caller drives the driver-session inline via ``MessageBus.request`` below,
    # but a Ctrl-C reaches ``Session.cancel_inflight`` on the CALLER session and
    # (pre-fix) only cancelled the caller's OWN RouterLoopDriver — never the
    # spawned driver-session's ``PipelineExecutorDriver`` whose ``cancel_check``
    # the executor polls at each step boundary. The reply address is the caller's
    # own (agent, sid) by construction (see this function's contract), so it
    # resolves to the SAME live Session instance whose ``cancel_inflight`` the
    # human Ctrl-C fires. Register the driver's ``request_cancel`` as a
    # cancel-forward for the DURATION of the attached pump, unregistered in
    # ``finally`` so it never leaks past the run. Best-effort: if the caller
    # session is not resolvable (should not happen on the attached path — the
    # caller is live), skip the bridge (degrades to the pre-fix behavior, never
    # blocks the run).
    driver = getattr(session, "_loop_driver", None)
    unregister_cancel: "Any | None" = None
    if caller_session is not None and driver is not None:
        register = getattr(caller_session, "register_cancel_forward", None)
        if callable(register):
            unregister_cancel = register(driver.request_cancel)

    bus = MessageBus()
    try:
        # #2708 P3.1: the drained outbox is no longer inspected for a ``"presentation"``
        # message to forward (that #2707 interim is removed — present now rides the
        # inherited parent sink, see below). The request is still awaited for pump
        # quiescence; its return value is intentionally unused.
        await bus.request(
            session,
            kind="user",
            payload={"text": "", "chain_id": uuid.uuid4().hex},  # the D案 run nudge
            reply_to=SystemRef(),
            timeout=timeout if timeout is not None else _DEFAULT_AGENT_STEP_TIMEOUT_S,
        )
    finally:
        if unregister_cancel is not None:
            unregister_cancel()

    # #2707 interim REMOVED here (#2708 P3.1): the driver-session's ``present`` no longer
    # renders to the driver's OWN outbox to be forwarded post-hoc. Half-A binds the driver's
    # present sink to the PARENT's consumer at spawn (``SpawnBridgePresentationConsumer``,
    # via ``attached_parent_session`` above), so a ``present`` step reaches the parent chat
    # surface BY CONSTRUCTION — exactly once. Keeping the old drain-and-copy forward here
    # alongside the bridge would DOUBLE-deliver the presentation, so it is deleted, not
    # migrated.
    marker = read_result(run_dir)
    if marker is not None:
        return {
            "status": marker.get("status", "ok"),
            "run_id": rid,
            "output": marker.get("output"),
            "named_stores": marker.get("named_stores"),
            "error": marker.get("error"),
        }

    # Non-terminal after the attached pump returned (the timeout safety net, or a
    # pump that yielded early): do NOT lose the run. Flip to inbox delivery and
    # hand it to the detached pump — it will finish and deliver to the caller's
    # inbox. Preserves the "never silently lose an in-flight run" contract.
    driver = getattr(session, "_loop_driver", None)
    if driver is not None:
        driver._notify_reply = True  # noqa: SLF001 — same-module runtime flag
    registry.ensure_session_running(reply_to_agent, sid)
    return {"status": "running_async", "run_id": rid}
