"""Programmatic session-spawn + run+collect entry points for non-LLM callers.

``AgentRegistry.spawn_session_recorded`` is the clean action-layer seam behind
``spawn_session`` (the LLM tool): it spawns a fresh-context session, persists +
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
     synchronous run+collect: put a ``TurnOrigin.AGENT_STEP`` message (the
     prompt is a model's reading material, never an operator's typed line, so
     it does not claim the ``user`` kind that slash dispatch acts on) on the
     spawned session's inbox, pump ``run_one_iteration`` on the caller's own task
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
    driver-isolation split. (Detached/async present has no attached parent surface — the
    #2708 P3-item3 completeness gate routes it ``AuditOnlyNoSurface``: audit-only, no orphan.)
    #2708 P3.2a: the SAME attach seam bridges the driver's INTERVENTION delivery. The driver
    gets a fresh listener-less ``InterventionRegistry`` (fail-closed), so an ``ask_user`` step
    would silently auto-refuse even with a live operator blocked on the parent (#2721). The
    attached driver-session is spawned with a ``SpawnBridgeInterventionListener``
    (``runtime/session_buses.py``) bound to the PARENT, so its router intervention bus
    dispatches on the PARENT session's live-operator listener — the operator is prompted and
    their answer flows back to the driver's awaiting op by construction. (Detached/async
    intervention has no attachable operator — the #2708 P3-item3 gate routes it
    ``AuditOnlyNoSurface``: a typed, reason'd refusal, replacing the pre-fix origin-pin park/hang.)

    #2708 P3-item3 (the spawn-axis completeness gate): every spawn seam
    (``spawn_session`` / ``spawn_session_recorded`` / ``spawn_ephemeral_session``) takes
    ``presentation_consumer`` + ``intervention_bridge`` as REQUIRED, no-default kwargs, so a
    spawn's user-reaching capabilities cannot silently self-bind. Each spawn site declares a
    ``runtime/spawn_routing`` decision: ``_spawn_pipeline_driver_session`` picks
    ``BridgeToParent`` (attached) or ``AuditOnlyNoSurface`` (detached); ``run_agent_step`` picks
    ``BridgeToParent(invoker_session)`` when a live invoking pipeline session is threaded in
    (#2769 — the agent-step's ask_user / permission / present reach the pipeline ORIGINATOR via
    the #2735 transitive bridge) or ``AuditOnlyNoSurface`` when detached / headless (no invoker —
    closing #2706, and fail-closed by construction).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from reyn.runtime.errors import AgentStepError
from reyn.runtime.session_pure import new_chain_id
from reyn.runtime.task_types import Requester
from reyn.runtime.transport import SystemRef
from reyn.runtime.turn_origin import TurnOrigin

if TYPE_CHECKING:
    from reyn.core.pipeline.schema import SchemaRegistry
    from reyn.runtime.registry import AgentRegistry

# Tool names an ``agent`` pipeline step must never reach — a leaf worker (R6
# session-hierarchy constraint 4: "E_i are spawn-tree LEAVES").
#   - ``run_pipeline`` (R6 S3): nesting a pipeline launch inside an ``agent``
#     step would let a step spawn ANOTHER pipeline at runtime, defeating the
#     transitive-closure cost-bound approval a pipeline gets at launch time —
#     nesting is ``call``-only. Proposal 0067 P7 (#3978) unified the former
#     4 pipeline-launch names (run_pipeline / run_pipeline_async /
#     run_pipeline_inline / run_pipeline_inline_async) into this one; every
#     collect=/definition= combination is STILL non-grantable inside a
#     pipeline step (an ad-hoc inline pipeline gets no exemption). Kept in
#     lock-step with ``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS`` (the
#     tool-step sibling of this agent-step deny) — see
#     ``test_pipeline_step_deny_sets_are_equal`` (tests/tools/
#     test_pipeline_step_deny_gate_3978.py), the equality gate architect
#     required after this arc's rebase collided on this exact pair.
#   - ``delegate_to_agent`` (retired, proposal 0067 P6, #3978) used to be the
#     other member here, for a DIFFERENT reason than run_pipeline's: a
#     mid-turn delegation would make ``MessageBus.request``'s quiescence
#     predicate (inbox.empty()) return early on a pending chain the spawned
#     session was still awaiting a reply for (see the module docstring).
#     That specific hazard has no live producer today — neither
#     ``run_prompt`` (synchronous, blocks in-band) nor ``send_to_session``
#     (fire-and-forget, never ends the turn) shares delegate_to_agent's
#     async-dispatch-ends-the-turn posture — so nothing currently needs this
#     deny-set entry for that reason. If a future tool reintroduces that
#     posture, it belongs back here.
# #3429: each name here is the tool's ONLY invocable name, so the deny-set is
# complete as written. It used to need ``_expand_tool_forms``
# (capability_profile.py) to add each tool's second, catalog-qualified spelling.
_DELEGATION_DENY_TOOLS: tuple[str, ...] = (
    "run_pipeline",
    # #4244: kept in lock-step with ``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS``
    # — see that set's own comment for the confused-deputy reason (an LLM-
    # authored pipeline `tool: hooks_add` step, run by a wider-authority
    # principal via reyn pipe run's session-less context, writing into the
    # shared GLOBAL hooks.yaml). A DIFFERENT hazard shape than
    # ``run_pipeline``'s R6 S3 nesting reason, but the two sets are required
    # to stay equal (``test_pipeline_step_deny_sets_are_equal``) so a tool
    # denied in one dispatch context is denied in the other too.
    "hooks_add",
)

# MessageBus.request has no default — an agent step needs one so callers
# aren't forced to pick a number for the common case.
_DEFAULT_AGENT_STEP_TIMEOUT_S: float = 120.0


async def spawn_ephemeral_session(
    registry: "AgentRegistry", *, identity: str, narrowing: "dict | None" = None,
    presentation_consumer: "object | None",
    intervention_bridge: "object | None",
) -> str:
    """Spawn an ephemeral session under ``identity`` for a non-LLM caller.

    Thin, direct wrapper over ``registry.spawn_session_recorded(identity,
    mode="ephemeral", narrowing=narrowing)`` — the same call the
    ``spawn_session`` tool's handler reaches via ``spawn_session_fn``, so the
    emitted ``session_spawned`` WAL event + the spawned session's narrowing
    enforcement are byte-identical to the tool path. Returns the new session id
    (the ``session_spawned`` event's ``sid``).

    No task is submitted here — that stays the caller's job (the Pipeline
    executor's ``agent`` step, in the eventual wiring), same as the S1bc
    action-layer seam does not submit either.

    #2708 P3-item3: ``presentation_consumer`` + ``intervention_bridge`` are REQUIRED, no-default
    kwargs (root-cause-i of #2706: this seam wholly lacked them, so an agent-step worker's
    ``present`` silently self-bound to an undrained outbox). The caller declares a
    ``runtime/spawn_routing`` decision — ``run_agent_step`` passes ``AuditOnlyNoSurface`` (a
    headless leaf worker has no attachable surface) — and this forwards it to
    ``spawn_session_recorded``."""
    return await registry.spawn_session_recorded(
        identity, mode="ephemeral", narrowing=narrowing,
        presentation_consumer=presentation_consumer,
        intervention_bridge=intervention_bridge,
    )


def _build_agent_step_narrowing(
    capabilities: "list[str] | None", parent_narrowing: "dict | None" = None,
) -> "dict | None":
    """The per-session narrowing an ``agent`` step spawns under.

    ``tool_deny`` always includes ``_DELEGATION_DENY_TOOLS`` — a v1
    structural constraint (R5), not something the caller's ``capabilities``
    can re-open: ``capability_profile`` resolution is deny-always-wins
    (``profile_permits``: ``in_allow and tool not in tool_deny``), so even a
    ``capabilities`` list that names a delegation tool is denied at the live
    gate. ``tool_allow`` is set only when the caller passes an explicit
    ``capabilities`` list.

    ``parent_narrowing`` (#3553) is the INVOKER's own sid-keyed #2103-S1a mapping
    (``AgentRegistry.per_session_narrowing``), composed in through
    ``capability_profile.compose_narrowing_mappings`` (deny ∪, allow ∩, absent
    allow = ⊤). ⚠️ This docstring used to say that omitting ``capabilities``
    "leaves the agent's normal envelope untouched (restrict-only narrowing, never
    a re-grant)". That was true only against the AGENT's own declaration, which is
    name-keyed and therefore re-derived on the worker for free — and false against
    the INVOKER, which is what a reader checks it for: the worker is born under a
    FRESH sid, so before #3553 the invoker's sid-keyed narrowing resolved to
    nothing on it and the two keys built here were the child's WHOLE envelope. An
    invoker narrowed to ``tool_allow: [A]`` therefore handed a ``capabilities``-less
    agent step a worker with no allow-list at all — a widening, not a restriction,
    and measured (``tests/runtime/test_3553_agent_step_worker_narrowing_inheritance.py``) as
    a denied tool's real side effect happening inside an agent step. The
    composition, not this function's own two keys, is what makes the claim true now,
    and only for the layers ``per_session_narrowing`` carries — see
    ``run_agent_step``'s call site for which layers those are.

    Returns ``None`` when there is nothing to impose at all, which cannot happen
    today (``_DELEGATION_DENY_TOOLS`` is non-empty) but keeps the return type the
    same as the composition's."""
    from reyn.security.permissions.capability_profile import compose_narrowing_mappings

    narrowing: dict[str, Any] = {"tool_deny": list(_DELEGATION_DENY_TOOLS)}
    if capabilities is not None:
        narrowing["tool_allow"] = list(capabilities)
    return compose_narrowing_mappings(parent_narrowing, narrowing)


async def run_agent_step(
    registry: "AgentRegistry",
    *,
    identity: str,
    prompt: str,
    capabilities: "list[str] | None" = None,
    schema: "str | None" = None,
    schema_registry: "SchemaRegistry | None" = None,
    model: "str | None" = None,
    chain_id: "str | None" = None,
    timeout: "float | None" = None,
    invoker_session: "Any | None" = None,
) -> Any:
    """Spawn an ephemeral session, run one turn, collect + return its output.

    The future Pipeline executor's ``agent`` step primitive (R5): spawn a
    leaf-worker session under ``identity`` (capability-narrowed to
    ``capabilities`` plus a structural delegation deny, see
    ``_build_agent_step_narrowing``), feed it ``prompt`` as a single
    ``TurnOrigin.AGENT_STEP`` turn via ``MessageBus.request``, and return its
    collected reply.

    #3595 step 1: that kind used to be ``"user"``, i.e. the prompt claimed a
    human had typed it at a client, which is what made every registered slash
    command executable from a model's output (``Session._handle_user_message``
    handed a ``/``-prefixed line to slash dispatch before any router turn; S5
    deleted that entry, so no inbox text is interpreted at all now). See
    ``TurnOrigin.AGENT_STEP``.

    With ``schema`` unset, returns the joined ``kind="agent"`` reply text
    verbatim. With ``schema`` set (a name registered in ``schema_registry``):
    0062 upgrades this from post-hoc-validate-only to ALSO constraining
    generation — the ephemeral session's answer turn is configured
    (``RouterLoopDriver.configure_structured_output``, before the turn is
    driven) to pass a ``response_format`` built from the named schema
    (``core.pipeline.schema.to_json_schema``), so the model's reply is
    provider-constrained JSON rather than free-formed text. The reply text is
    still JSON-parsed + validated here afterwards (belt-and-suspenders — the
    provider constraint is not blindly trusted). A ``schema`` without a
    ``schema_registry``, non-JSON text, or a schema-non-conforming value each
    raise ``AgentStepError`` — a normal step failure for the executor's
    retry/error path, not a construction-time error. An unsupported model /
    a provider-rejected schema / an exhausted re-prompt budget raise one of
    ``StructuredOutputUnsupportedModelError`` / ``StructuredOutputSchemaRejectedError``
    / ``StructuredOutputNonConformingError`` (all ``AgentStepError`` subtypes —
    see ``runtime.errors``), propagated from the turn itself.

    ``model`` (0062 layer 2, ``AgentStep.model``): an optional model-CLASS
    override for the ephemeral session's answer turn, applied the same way
    the ``/model`` slash command overrides a session's model
    (``session._model_override``) — resolved via the session's own
    ``ModelResolver`` at call time, exactly like every other model-class
    field in the codebase (no bespoke resolution path).

    ``chain_id`` defaults to a fresh uuid4 hex (mirrors ``MessageBus``'s own
    ``_new_request_id``). ``timeout`` defaults to
    ``_DEFAULT_AGENT_STEP_TIMEOUT_S`` seconds.

    ``invoker_session`` (#2769) is the LIVE session that invoked this pipeline run
    — the pipeline driver-session, threaded down opaquely by ``PipelineExecutor``
    from ``PipelineExecutorDriver`` (which holds ``self._session``). When present,
    the ephemeral worker's user-reaching capabilities route ``BridgeToParent`` to
    it, so an agent-step ``ask_user`` / permission JIT approval / ``safety.limit`` /
    MCP elicitation AND ``present`` reach the pipeline ORIGINATOR (the operator) —
    the #2735 compositional transitive bridge walks agent-step → driver → root
    operator, resolving at the first attached ancestor. When the invoking pipeline
    was itself launched DETACHED, the driver-session's own bridge is
    ``AuditOnlyInterventionBridge`` and that same transitive walk terminates in a
    typed refusal at the driver hop — so the fail-closed behavior is preserved by
    the driver's own routing, not by this seam. ``None`` (a CLI-headless ``reyn
    pipe`` run with no live session, or a direct executor call) routes
    ``AuditOnlyNoSurface`` here directly.
    """
    from reyn.core.pipeline.schema import to_json_schema, validate
    from reyn.runtime.message_bus import MessageBus
    from reyn.runtime.spawn_routing import AuditOnlyNoSurface, BridgeToParent

    # Moved ahead of the spawn (was previously checked only after the turn ran):
    # a ``schema`` without a ``schema_registry`` is a caller-contract error that
    # can never succeed — failing before spawning the ephemeral session avoids
    # wasting a spawn (+ its S5 budget charge) on a call that cannot complete.
    if schema is not None and schema_registry is None:
        raise AgentStepError(
            f"run_agent_step(schema={schema!r}) requires schema_registry "
            "(no registry to validate against)."
        )

    # #3553: the leaf worker is a NEW permission envelope, born under a FRESH sid, so
    # the invoker's sid-keyed #2103-S1a narrowing does not reach it by identity the way
    # the name-keyed layers do (the agent's own ``permissions`` declaration, its topology
    # ``capability_profile`` bindings, the #2081 ``_delegate`` floor — all re-derived from
    # the agent NAME by ``resolved_profile_for``). It has to be read back off the invoker
    # and composed with this step's own narrowing, or a ``capabilities``-less agent step
    # hands the worker a strictly WIDER envelope than the session that asked for it. This
    # is the same seam #3546/#3554 closed one level up, for the pipeline driver-session
    # that is typically the ``invoker_session`` here; the two layers the sibling site
    # deliberately does NOT carry are unchanged (the #2285 in-memory ``/visibility``
    # toggle and the #1827-S4b ephemeral untrusted-context narrowing, which
    # ``Session._ephemeral_contextual_for_turn`` re-derives per turn from the session's
    # OWN history and so is not an inheritable value at all).
    parent_narrowing = (
        registry.per_session_narrowing(
            invoker_session.agent_name, invoker_session.session_id,
        )
        if invoker_session is not None
        else None
    )
    narrowing = _build_agent_step_narrowing(capabilities, parent_narrowing)
    # #2769 (refines #2706/#2710 P3-item3): an agent-step's user-reaching capabilities route to the
    # pipeline INVOKER when one is threaded in (``BridgeToParent(invoker_session)`` — the driver
    # session), so ``ask_user`` / permission / ``safety.limit`` / elicitation AND ``present`` reach
    # the originating operator via the #2735 transitive bridge (agent-step → driver → operator). With
    # NO invoker (CLI-headless ``reyn pipe`` / a direct executor call), route ``AuditOnlyNoSurface`` —
    # ``present`` is audit-only (durable ``presented`` P6 event; no orphan outbox) and ``ask_user``
    # returns a typed refusal, never a silent self-bind/hang. When the invoker pipeline is DETACHED,
    # the driver session's OWN bridge is AuditOnly, so the transitive walk still refuses fail-closed —
    # the DENY is a consumer-side deny-by-default property, independent of this routing decision.
    routing = (
        BridgeToParent(invoker_session)
        if invoker_session is not None
        else AuditOnlyNoSurface()
    )
    sid = await spawn_ephemeral_session(
        registry, identity=identity, narrowing=narrowing,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = registry.get_session(identity, sid)
    if session is None:
        raise AgentStepError(
            f"run_agent_step: spawn_ephemeral_session({identity!r}) returned "
            f"sid={sid!r}, but registry.get_session({identity!r}, {sid!r}) "
            "found no live session — the registry's session_factory may not "
            "register the spawned session under its own name/sid."
        )

    # 0062 layer 1/2: configure THIS turn's answer to be schema-constrained
    # (response_format) and/or override the model class, BEFORE driving the
    # turn — mirrors the ``_loop_observer`` Tier-2 seam's "configure the
    # constructed session before its turn" shape, but is production wiring:
    # every other Session (chat, pipeline driver, ...) never calls
    # ``configure_structured_output`` / sets ``_model_override`` from here, so
    # this is a no-op (byte-identical) for every non-schema, non-model-override
    # agent step.
    if schema is not None:
        json_schema = to_json_schema(schema, schema_registry)
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema, "schema": json_schema},
        }

        def _validate_fn(parsed_value: Any) -> "list[str]":
            result = validate(parsed_value, schema, schema_registry)
            return [
                f"{e.path or '<root>'}: {e.message}" for e in result.errors
            ]

        session._loop_driver.configure_structured_output(  # noqa: SLF001 — production seam (RouterLoopDriver.configure_structured_output)
            response_format=response_format,
            schema_validate_fn=_validate_fn,
        )
    if model is not None:
        # Same override point the ``/model`` slash command uses
        # (``interfaces/slash/model.py``) — a model-CLASS string, resolved by
        # the session's own ``ModelResolver`` at call time, not a bespoke path.
        session._model_override = model  # noqa: SLF001

    bus = MessageBus()
    replies = await bus.request(
        session,
        kind=TurnOrigin.AGENT_STEP,
        payload={"text": prompt, "chain_id": chain_id or new_chain_id()},
        reply_to=SystemRef(),
        timeout=timeout if timeout is not None else _DEFAULT_AGENT_STEP_TIMEOUT_S,
    )
    # #2708 P3-item3 (#2706 root-cause-ii) + #2769: a ``present`` step's output is ROUTED per the
    # worker's DECLARED consumer — under ``BridgeToParent`` (attached invoker) it renders onto the
    # INVOKER's outbox (reaching the operator by construction); under ``AuditOnlyNoSurface`` (detached)
    # it renders audit-only (the durable ``presented`` P6 event). In EITHER case it renders at op time
    # at the worker's own sink and never arrives here as a ``"presentation"`` outbox reply for this
    # ``kind == "agent"`` filter to see — so present symmetry falls out of the ROUTING decision above,
    # NOT of this filter (do NOT special-case it for present). The join is the agent-step's RETURN
    # text; presentation is delivered/audited per the declaration, never lost here.
    text = "\n\n".join(r.text for r in replies if r.kind == "agent")

    if schema is None:
        return text

    # (schema_registry is None already rejected above, before the spawn.)
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
    on_settle: str = "deliver",
) -> "tuple[Any, str, str]":
    """Spawn + arm a pipeline driver-session, up to (but NOT including) the
    run/resume nudge — the shared launch prefix of the async (``start_pipeline_run``)
    and sync-attached (``run_pipeline_attached``) paths.

    #3097 (folds out #3094's spawn-local override): the freshly-spawned
    driver-session's ``PipelineRegistry`` no longer needs an explicit hand-off
    from the caller. ``registry.spawn_session_recorded`` below (the single
    funnel every programmatic spawn shares) now fires
    ``Session.refresh_config_projections()`` on the newly-spawned session
    BEFORE this function returns — which includes the ``pipelines`` seam
    (``Session._reapply_pipelines``), rebuilding the driver-session's OWN
    registry from the CURRENT on-disk config cascade uniformly, the same
    mechanism a live ``/reload`` uses. This is equivalent to (and supersedes)
    #3093/#3094's point-fix: a pipeline installed mid-conversation writes to
    disk BEFORE the spawn that runs it (confirmed topology, #3061), so the
    driver-session's own fresh rebuild finds it without needing the caller to
    have already hot-reloaded its own in-memory copy. See
    ``Session.refresh_config_projections``'s docstring for the full family-gate
    mechanism and ``Session._reapply_pipelines`` for the pipeline-specific
    rebuild.

    In crash-safety order:

      1. spawn the driver-session under the INVOKER's identity
         (``spawn_session_recorded(mode="persistent")`` — the same recorded seam
         as every other programmatic spawn; persistent because the session must
         survive a crash to be re-woken), passing the invoker's per-session
         ``narrowing``.
         ⚠️ Identity alone does NOT reproduce the invoker's envelope, though
         this step used to say it did ("⊆ by construction"). Identity carries
         the NAME-keyed layers — the agent's ``permissions`` declaration, its
         topology ``capability_profile`` bindings, the #2081 ``_delegate``
         floor — because ``resolved_profile_for`` re-derives those from the
         agent name. The #2103-S1a per-session narrowing is keyed by SID: it
         lives in ``<session-state-dir>/config.yaml``, and a freshly-spawned
         driver-session has a fresh sid and therefore no such file. So it had
         to be handed over explicitly, and is (``registry.per_session_narrowing``
         → ``narrowing=``), which is what the two sibling ``spawn_session_recorded``
         call sites already did (#3546). Two further layers are deliberately not
         carried: the #2285 in-memory ``/visibility`` toggle (an operator view
         override on a live ``Session``, not persisted and not passed at the
         sibling sites either) and the #1827-S4b ephemeral untrusted-context
         narrowing (not inheritable state at all — ``Session.
         _ephemeral_contextual_for_turn`` re-derives it each turn from that
         session's OWN history plus the ``safety.threat_scan.capability_narrowing``
         opt-in).
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

    #2708 P3-item3: the spawn-time routing is a typed decision. ATTACHED path (given
    ``attached_parent_session`` — ``run_pipeline_attached`` passes the live caller):
    ``BridgeToParent`` binds the driver's present sink + ask_user routing to the PARENT, so a
    ``present``/``ask_user`` step reaches the parent surface/operator by construction. DETACHED
    path (``start_pipeline_run``, ``attached_parent_session=None``): ``AuditOnlyNoSurface`` —
    ``present`` is audit-only (durable ``presented`` P6 event; no orphan outbox, closing #2710)
    and ``ask_user`` returns a typed refusal (closing the confirmed detached HANG), a DELIBERATE
    reviewed fail-mode rather than the pre-fix incidental self-bound orphan/park."""
    from reyn.core.events.config_recovery import reyn_root
    from reyn.core.pipeline.serde import pipeline_to_dict
    from reyn.core.pipeline.work_order import (
        PipelineWorkOrder,
        pipeline_run_dir,
        write_invocation,
    )
    from reyn.runtime.services.pipeline_executor_driver import (
        PipelineExecutorDriver,
        resolve_reply_target,
    )

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
    # #2708 P3-item3: the driver's spawn-time user-reaching routing is an explicit, typed decision
    # (``runtime/spawn_routing``). ATTACHED path (``run_pipeline_attached`` passes the live caller):
    # ``BridgeToParent`` — a ``present`` step reaches the parent surface (P3.1) and an ``ask_user``
    # step reaches the parent's live operator listener (P3.2a, #2721), both by construction. DETACHED
    # path (``start_pipeline_run``, no attached surface): ``AuditOnlyNoSurface`` — ``present`` is
    # audit-only (durable ``presented`` P6 event; no orphan outbox — closes #2710) and ``ask_user``
    # returns a typed refusal instead of parking forever (closes the confirmed detached HANG).
    from reyn.runtime.spawn_routing import AuditOnlyNoSurface, BridgeToParent

    routing = (
        BridgeToParent(attached_parent_session)
        if attached_parent_session is not None
        else AuditOnlyNoSurface()
    )
    # #3546: the driver-session is where a NEW permission envelope is born, so the
    # invoker's sid-keyed narrowing has to be handed to it explicitly — sharing the
    # invoker's IDENTITY re-derives only the name-keyed layers (see this function's
    # docstring, step 1). Sibling parity: the two other ``spawn_session_recorded``
    # call sites (``spawn_session``'s router host, the ``agent`` step) already pass
    # ``narrowing=``; this was the one that did not.
    sid = await registry.spawn_session_recorded(
        reply_to_agent, mode="persistent",
        narrowing=registry.per_session_narrowing(reply_to_agent, reply_to_sid),
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
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
        # Proposal 0067 P7 (#3978): the first caller-supplied value this field
        # ever receives (WorkOrder.on_settle's own docstring: "P7's job").
        # The attached path (run_pipeline_attached, below) never passes this
        # param — its call keeps the dataclass default "deliver", which is
        # correct-by-construction since ADR-0040 D4 already established the
        # attached path creates no settle handle at all, so nothing ever
        # reads this field for that run.
        on_settle=on_settle,
    )
    write_invocation(pipeline_run_dir(root, rid), work_order)
    session = registry.get_session(reply_to_agent, sid)
    if session is None:
        raise RuntimeError(
            f"pipeline launch: spawned driver-session ({reply_to_agent!r}, "
            f"{sid!r}) not found in the registry"
        )
    if attached_parent_session is not None:
        # #4215 ②: bridge this driver's hook-bus events to the PARENT's
        # bus, non-blocking, same ATTACHED-only condition as the
        # presentation/intervention BridgeToParent routing above — a
        # detached spawn (AuditOnlyNoSurface) has no live parent to bridge
        # to. See bridge_child_bus_to_parent's own docstring for why this
        # goes through neither session's HookDispatcher, and Session.
        # _hook_bus_bridge_task for where teardown cancels it.
        from reyn.hooks.bus import bridge_child_bus_to_parent

        # #4759: registered with the owning session's task funnel
        # (tracked_tasks.py) so AgentRegistry.shutdown() reaches it directly
        # instead of only via remove_session's bare (never-awaited) .cancel()
        # — see Session._hook_bus_bridge_task's own note above.
        # appends_wal=False (the default, stated explicitly here): this
        # bridges hook events for the LIFE of the driver session and does
        # not itself append to the WAL -- a mid-rewind quiesce must not
        # tear it down while the session keeps running.
        session._hook_bus_bridge_task = session._background_tasks.spawn(
            bridge_child_bus_to_parent(session._hook_bus, attached_parent_session._hook_bus),
            disposition="cancel_join", appends_wal=False, name="hook-bus-bridge",
        )
    driver = PipelineExecutorDriver(
        work_order, registry=registry, state_log=state_log,
        notify_reply=notify_reply,
    )
    if notify_reply:
        # proposal 0067 settle path (#3978): register this run's collection
        # handle on the REPLY session's own ChainManager (pending_chains,
        # repurposed per the proposal's own P6 note) BEFORE the driver ever
        # runs — settle-time (``PipelineExecutorDriver._deliver``) pops it
        # via the SAME ``resolve_reply_target`` this call mirrors. The sync
        # ATTACHED path (``notify_reply=False``) creates no handle at all
        # (ADR-0040 D4: "collect='attached' creates nothing — nothing to
        # retain, on_settle is ignored"). A resolution failure here is
        # non-fatal — it just means no handle exists yet (the same "no
        # handle" case ``ChainManager.settle()`` already tolerates); the
        # run still launches and ``_deliver``'s own fail-safe re-discovers
        # the vanished target at settle time.
        #
        # proposal 0067 P4 (#3978), architect ruling 2026-08-10: the
        # DRIVER's own ``request_cancel`` (the SAME argument-zero hook
        # ``run_pipeline_attached`` already forwards via
        # ``register_cancel_forward`` for the sync path) is captured here
        # as this handle's ``cancel`` — the async path had NO cancel
        # reachability before this (nothing forwards a cancel signal to a
        # detached driver-session; only an ATTACHED caller's
        # cancel_inflight ever reached ``request_cancel``). ``cancel_task``
        # reads this off the handle; it does NOT branch on ``kind`` to
        # pick a mechanism (architect's correction — the three
        # cancellation mechanisms differ in what LIVE OBJECT they need,
        # not in which method name to call).
        reply_target, _reason = await resolve_reply_target(
            registry, reply_to_agent, reply_to_sid,
        )
        if reply_target is not None:
            await reply_target.chains.register(
                chain_id=rid, depth=0,
                original_text=pipeline_name, sender=None,
                requester=Requester(agent_name=reply_to_agent, session_id=reply_to_sid),
                kind="pipeline",
                cancel=driver.request_cancel,
            )
    session.set_loop_driver(driver)
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
    on_settle: str = "deliver",
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

    #3097: the driver-session's OWN pipeline registry is refreshed at ITS
    spawn by the config-projection family gate (``spawn_session_recorded`` ->
    ``Session.refresh_config_projections()``) — no caller hand-off needed. See
    ``_spawn_pipeline_driver_session``'s docstring for the full mechanism.

    ``on_settle`` (proposal 0067 P7, #3978): "deliver" (default) | "<pipeline
    name>" | "drop" — the first caller-supplied value this ever receives; see
    ``PipelineWorkOrder.on_settle``'s own docstring. Threaded straight to
    ``_spawn_pipeline_driver_session``, which stamps it onto the work-order
    the settle path (``ChainManager.settle`` / ``PipelineExecutorDriver.
    _deliver``) reads at completion time.

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
        on_settle=on_settle,
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

    # #3097: no explicit pipeline_registry hand-off needed — the driver-session's
    # OWN registry is refreshed at ITS spawn by the config-projection family gate
    # (spawn_session_recorded -> Session.refresh_config_projections(), which
    # includes the pipelines seam). Folds out #3094's caller_pipeline_registry
    # forwarding. See _spawn_pipeline_driver_session's docstring for the mechanism.
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
        # #3595 S2: this pump used to claim ``CLIENT_INPUT``. It was the fifth and
        # last producer to do so, and the only one the slash defect could never
        # expose — its text is ``""``, so ``startswith("/")`` was never true — which
        # is precisely why four censuses walked past it: nothing it did was wrong.
        # The claim was still false. Nobody authored this message; it exists to hand
        # the driver-session's executor one iteration, so it says that instead.
        await bus.request(
            session,
            kind=TurnOrigin.PIPELINE_NUDGE,
            payload={"text": "", "chain_id": new_chain_id()},  # the D案 run nudge
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


async def run_prompt_result(
    registry: "AgentRegistry",
    *,
    caller_agent: str,
    caller_sid: str,
    target_agent: str,
    target_session: str,
    prompt: str,
    timeout: float,
    schema: "str | None" = None,
    schema_registry: "SchemaRegistry | None" = None,
) -> dict:
    """proposal 0067 P4d (#3978): ``run_prompt(collect="attached")`` — deliver
    ``prompt`` to a LIVE peer ``(target_agent, target_session)`` as a
    ``TurnOrigin.PEER_SESSION`` message and collect its reply IN-BAND,
    synchronously, via the SAME run+collect primitive ``run_agent_step`` uses
    (``MessageBus.request``, pumping ``run_one_iteration`` on the CALLER's own
    task).

    Unlike ``run_agent_step``/``run_pipeline_attached``, the target here is
    NOT a session THIS call spawns and therefore exclusively owns — it
    addresses an existing peer, the same ``(agent, session)`` shape
    ``send_to_session`` uses. Two refusals follow directly from that,
    per architect's #3978 ruling (2026-08-10), and are NOT edge cases to
    soften later — each maps to a real invariant this codebase already
    states elsewhere:

    1. **No live session found → refuse, never spawn.** ADR-0040 D5's own
       precedent for ``send_to_session``: "tap the shoulder, not a spawn
       primitive" — a target naming no LIVE session returns an error rather
       than loading/spawning one. Resolution mirrors
       ``Session._deliver_cross_session_message`` (colon-prefixed transport
       ids resolve via ``registry.resolve_session``; a plain sid resolves via
       ``registry.get_session``).
    2. **A target ALREADY self-running its own turn loop → refuse, never
       drive.** ``MessageBus.request`` pumps ``run_one_iteration`` on the
       CALLER's task; reyn's own invariant (stated explicitly at the A2A
       router: "a session is EITHER self-running OR inline-driven, never
       both") would break if this call raced a background
       ``ensure_session_running`` task pumping the SAME Session. Checked via
       ``AgentRegistry.is_session_running`` — architect's ruling (2026-08-10)
       is explicit that this check is INTERIM: the durable fix belongs to
       issue #4113 (a registry-owned "who is driving this session right now"
       marker covering BOTH the self-running axis and this one), not to a
       feature PR. Do not read this as the finished mechanism — #4113 will
       replace it.
    3. **Two CONCURRENT ``run_prompt`` calls targeting the SAME (idle) peer**
       — a DIFFERENT race than #2 (neither side is self-running, so
       ``is_session_running`` sees both as free). Closed by the EXISTING
       ``get_agent_lock(agent, sid)`` — unlike axis #2, BOTH callers here are
       "the one acquiring", which is exactly what an ``asyncio.Lock``
       serializes; the production MCP path (``mcp/server.py:255``) already
       holds the SAME lock across its own ``MessageBus.request``, so
       acquiring it here also serializes ``run_prompt`` against a
       concurrent MCP call on the same peer, for free.

       ⚠️ **This closes double-pump but OPENS a mutual-deadlock shape**
       (architect's correction, #3978, 2026-08-10): if session A's turn
       (holding ``lock(A)``) calls ``run_prompt`` targeting B while B's turn
       (holding ``lock(B)``) calls ``run_prompt`` targeting A, each waits on
       the other's lock while holding its own — classic AB/BA deadlock.
       ``asyncio.Lock`` has no native timeout, so nothing but an EXTERNAL
       bound can ever resolve this. That is why ``timeout`` is REQUIRED
       here (not optional the way ``run_agent_step``'s is) and why it wraps
       the LOCK ACQUISITION too, not just the pump below — a timeout that
       only bounded ``MessageBus.request`` would leave the lock-wait itself
       unbounded, i.e. would not actually close the deadlock it exists to
       bound.

    ``schema`` mirrors ``run_agent_step``'s 0062 contract exactly: constrains
    generation (``configure_structured_output``) AND validates the parsed
    reply post-hoc; raises ``AgentStepError`` on non-JSON or non-conforming
    output. A ``schema`` without ``schema_registry`` is rejected before
    touching the target at all (same ordering as ``run_agent_step`` — no
    point resolving/refusing a peer for a call that can never complete).

    Returns ``{"status": "ok", "result": <str|parsed-json>}`` on success, or
    ``{"status": "error", "kind": ..., "error": ...}`` for either refusal —
    never a success-shaped envelope for a prompt that was never delivered
    (same B33 W5 F2 discipline ``delegate_to_agent``/``send_to_session``
    follow: a silently-absent reply invites the LLM to fabricate one on the
    peer's behalf)."""
    import asyncio

    from reyn.core.pipeline.schema import to_json_schema, validate
    from reyn.runtime.agent_locks import get_agent_lock
    from reyn.runtime.message_bus import MessageBus

    if schema is not None and schema_registry is None:
        raise AgentStepError(
            f"run_prompt(schema={schema!r}) requires schema_registry "
            "(no registry to validate against)."
        )

    try:
        # architect ruling (#3978, 2026-08-10, "axis B" + the deadlock
        # correction that followed it same day): the SAME lock the
        # production MCP path takes (mcp/server.py:255) around its own
        # resolve + MessageBus.request + history-read, keyed by the TARGET
        # (agent, sid) — serializes two concurrent run_prompt calls (or a
        # run_prompt racing an MCP call) against the SAME peer, closing the
        # double-pump race. Does NOT cover the self-running axis (#2 above /
        # issue #4113) — a session's own run-loop never acquires this lock.
        #
        # ``asyncio.timeout`` wraps the LOCK ACQUISITION as well as the pump
        # (not just ``bus.request`` below) — this is what actually closes
        # the mutual-deadlock shape #3 describes: A holding lock(A) while
        # waiting on lock(B), B holding lock(B) while waiting on lock(A).
        # ``asyncio.Lock`` has no native timeout; an outer deadline around
        # the acquire is the ONLY thing that can ever un-stick that wait.
        async with asyncio.timeout(timeout):
            async with get_agent_lock(target_agent, target_session):
                if ":" in target_session:
                    transport, _, native = target_session.partition(":")
                    target = registry.resolve_session(target_agent, transport, native)
                else:
                    target = registry.get_session(target_agent, target_session)
                if target is None:
                    return {
                        "status": "error",
                        "kind": "target_session_not_found",
                        "error": (
                            f"no live session ({target_agent!r}, {target_session!r}) — "
                            'run_prompt(collect="attached") addresses an already-running '
                            "peer, the same as send_to_session; it does not spawn one."
                        ),
                    }

                if registry.is_session_running(target_agent, target_session):
                    return {
                        "status": "error",
                        "kind": "target_session_busy",
                        "error": (
                            f"({target_agent!r}, {target_session!r}) is currently "
                            'running its own turn loop — run_prompt(collect="attached") '
                            "cannot drive it inline without double-pumping the same "
                            "session; retry once it is idle, or use send_to_session "
                            "instead."
                        ),
                    }

                if schema is not None:
                    json_schema = to_json_schema(schema, schema_registry)
                    response_format = {
                        "type": "json_schema",
                        "json_schema": {"name": schema, "schema": json_schema},
                    }

                    def _validate_fn(parsed_value: Any) -> "list[str]":
                        result = validate(parsed_value, schema, schema_registry)
                        return [
                            f"{e.path or '<root>'}: {e.message}" for e in result.errors
                        ]

                    target._loop_driver.configure_structured_output(  # noqa: SLF001 — production seam (RouterLoopDriver.configure_structured_output)
                        response_format=response_format,
                        schema_validate_fn=_validate_fn,
                    )

                bus = MessageBus()
                replies = await bus.request(
                    target,
                    kind=TurnOrigin.PEER_SESSION,
                    payload={
                        "text": prompt,
                        "chain_id": new_chain_id(),
                        "from_agent": caller_agent,
                        "from_session": caller_sid,
                        "sender": f"peer_session:{caller_agent}/{caller_sid}",
                        "name": f"{caller_agent}/{caller_sid}",
                    },
                    reply_to=SystemRef(),
                    timeout=timeout,
                )
                text = "\n\n".join(r.text for r in replies if r.kind == "agent")
    except TimeoutError:
        # architect ruling (#3978, 2026-08-10, follow-up measurement): do
        # NOT claim a mutual-lock deadlock was DETECTED here — no
        # discriminator for "who/what this turn is waiting on" actually
        # exists today (CurrentTask.requester is always None at its one
        # construction site; _PendingChain.requester doesn't cover an
        # MCP-originated turn either, since a chain is per-relay, not
        # per-turn). Naming a specific cause this call cannot actually tell
        # apart from an ordinary slow reply would be a false claim. Name
        # only WHAT was being waited on (the lock acquisition + the peer's
        # reply, as one combined budget) — never WHY it didn't arrive.
        return {
            "status": "error",
            "kind": "timeout",
            "error": (
                f'run_prompt(collect="attached") to ({target_agent!r}, '
                f"{target_session!r}) did not complete within {timeout}s "
                "(waiting on that peer's serialization lock and/or its "
                "reply — cause not distinguished)."
            ),
        }

    if schema is None:
        return {"status": "ok", "result": text}

    # (schema_registry is None already rejected above, before resolving the target.)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentStepError(
            f"run_prompt(schema={schema!r}): reply is not valid JSON: "
            f"{exc}. Output: {text!r}"
        ) from exc
    result = validate(parsed, schema, schema_registry)
    if not result.conforming:
        details = "; ".join(f"{e.path or '<root>'}: {e.message}" for e in result.errors)
        raise AgentStepError(
            f"run_prompt(schema={schema!r}): reply does not conform to "
            f"schema: {details}"
        )
    return {"status": "ok", "result": parsed}


async def run_prompt_async(
    registry: "AgentRegistry",
    *,
    caller_agent: str,
    caller_sid: str,
    target_agent: str,
    target_session: str,
    prompt: str,
) -> dict:
    """proposal 0067 P4e (#3978): ``run_prompt(collect="async")`` — dispatch
    ``prompt`` to a LIVE peer ``(target_agent, target_session)`` as an
    ``agent_request`` (the SAME transport ``delegate_to_agent`` used to
    call, via ``InterAgentMessaging.send_to_agent``) and return a
    ``task_id`` IMMEDIATELY — the reply arrives later via ``task_settled``,
    not in this call.

    Architect ruling (#3978, 2026-08-10, three rounds — reply-routing
    identity, the register-per-call structural condition, and this
    function's own shape):

    **Producer identity**: the TOOL (``delegate_to_agent``) retired in P6;
    the SUBSTRATE (``ChainManager.register()``/``.settle()``, the WAL
    shape, journal, timeout arming) did not — P6 explicitly left it in
    place for this function to make live again. ``send_to_agent`` is
    reused ONLY as delivery transport here, not through its
    delegation-tracker/finally-block side channel (see the next point).

    **Why this function registers the chain directly, not via the
    existing ``dispatched``/finally accumulation** (``inter_agent_messaging.py``'s
    ``_handle_agent_request``/``_resolve_pending_chain``): that wrapper (1)
    only exists for agent-to-agent turn handling — it is never armed for a
    plain user-triggered turn, which is where an LLM actually calls
    ``run_prompt``, and (2) accumulates EVERY ``send_to_agent`` call across
    a whole turn into ONE chain at its ``finally`` block. Reusing it would
    let the LLM calling ``run_prompt(collect="async")`` twice in one turn
    produce a single ``waiting_on={B, C}`` chain tagged ``kind="prompt"`` —
    a JOIN wearing a TASK's kind, corrupting the |waiting_on| cardinality
    rule the task vocabulary depends on (architect: |waiting_on| == 1 is a
    prompt task, >= 2 is a join, by the chain's OWN shape, never by
    producer). Registering directly here, once per call, satisfies "1 call
    = 1 chain" STRUCTURALLY — there is no shared per-turn state for a
    second call to ever collide with, not merely "if careful."

    **task_id ≡ chain_id** (architect's ID-space ruling): no separate id is
    minted — the registered ``chain_id`` IS the returned ``task_id``,
    identical to how ``describe_task``/``list_tasks`` already read
    ``chain.chain_id`` as ``task_id`` for every other task kind.

    **``cancel``**: wraps the TARGET session's ``cancel_inflight()``
    (fire-and-forget — ``_PendingChain.cancel`` is a synchronous zero-arg
    hook, ``cancel_inflight`` is async) — ADR-0040 D3 gives a monitor
    ``cancel_task`` reach on a task; without this the registered handle's
    ``cancel`` would be ``None`` and ``cancel_task`` would correctly (not
    falsely) report "cannot cancel," but the task would then be
    permanently uncancellable, which the ADR does not intend.

    Refusals mirror ``run_prompt(collect="attached")``'s own two ADR-0040
    D5 refusals (target must be a LIVE session; never spawns one) — this
    function does NOT need the busy-check/lock/deadlock-timeout machinery
    ``run_prompt_result`` needs, because it never drives the target
    inline; the target keeps running its own turn loop untouched."""
    caller_session = registry.get_session(caller_agent, caller_sid)
    if caller_session is None:
        raise RuntimeError(
            f"run_prompt(collect=\"async\") requires a live caller session "
            f"({caller_agent!r}, {caller_sid!r}) — mis-wiring."
        )

    if ":" in target_session:
        transport, _, native = target_session.partition(":")
        target = registry.resolve_session(target_agent, transport, native)
    else:
        target = registry.get_session(target_agent, target_session)
    if target is None:
        return {
            "status": "error",
            "kind": "target_session_not_found",
            "error": (
                f"no live session ({target_agent!r}, {target_session!r}) — "
                'run_prompt(collect="async") addresses an already-running '
                "peer, the same as send_to_session; it does not spawn one."
            ),
        }

    import asyncio

    chain_id = new_chain_id()

    def _cancel_hook() -> None:
        # Fire-and-forget — _PendingChain.cancel is a synchronous zero-arg
        # hook, cancel_inflight() is async; mirrors run_pipeline_attached's
        # own cross-session cancel-forward registration.
        # #4759: registered on the TARGET session's own task funnel (it's
        # the target's cancel_inflight() running here, not the caller's) —
        # was a bare ensure_future with no reference kept anywhere.
        task = asyncio.ensure_future(target.cancel_inflight())
        task.set_name(f"cross-session-cancel-forward-{chain_id}")
        target_tracker = getattr(target, "_background_tasks", None)
        if target_tracker is not None:
            # appends_wal=False (the default, stated explicitly here): a
            # cancel-forward is a short-lived one-shot, unrelated to
            # rewind/WAL-quiesce semantics -- no reason for a mid-rewind
            # quiesce point to newly start touching it.
            target_tracker.register(task, disposition="cancel_join", appends_wal=False)

    await caller_session.chains.register(
        chain_id=chain_id,
        depth=1,
        original_text=prompt,
        sender=caller_agent,
        waiting_on={target_agent},
        requester=Requester(agent_name=caller_agent, session_id=caller_sid),
        origin_depth=1,
        kind="prompt",
        cancel=_cancel_hook,
    )
    await caller_session.chains.arm_timeout(
        chain_id, on_fire=caller_session._on_chain_timeout_fire,  # noqa: SLF001 — same shape as InterAgentMessaging's own register()+arm_timeout pairing (chain_manager.py callers)
    )

    await caller_session._send_to_agent(  # noqa: SLF001 — session_api reaches thin Session delegators the same way run_pipeline_attached's cancel-forward registration already does
        to=target_agent, request=prompt, depth=1, chain_id=chain_id,
    )
    return {"status": "started", "data": {"task_id": chain_id}}
