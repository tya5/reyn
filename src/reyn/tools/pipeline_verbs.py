"""Pipeline launch router tools — REGISTERED + ad-hoc INLINE, sync + async.

Per ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6: an agent
launches a pipeline and collects its result. This module hosts the four launch
verbs plus the tool-step dispatch they share:

  - ``run_pipeline`` / ``run_pipeline_async`` — launch a REGISTERED pipeline
    (pre-built via :class:`reyn.core.pipeline.registry.PipelineRegistry`) by
    name, sync-attached or fire-and-forget.
  - ``run_pipeline_inline`` / ``run_pipeline_inline_async`` (IS-4) — launch an
    ad-hoc, agent-GENERATED pipeline whose ``definition`` is a DSL STRING the
    agent produced at runtime (Appendix B grammar). The string is parsed
    (``reyn.core.pipeline.parser.parse_pipeline_dsl``, IS-3 — including any
    inline ``schema:`` documents in the same string, into a fresh per-call
    :class:`~reyn.core.pipeline.schema.SchemaRegistry`) into a ``Pipeline``,
    which then feeds the SAME downstream every registered launch uses. The
    inline verbs SKIP the registry entirely — the only extra machinery over the
    registered verbs is the parse ENTRY and a **static-analysis gate** (see
    :func:`_static_analysis_gate`) that runs BEFORE anything is spawned.

The inline + registered verbs converge immediately after the pipeline is in
hand: both call ``reyn.runtime.session_api.run_pipeline_attached`` (sync) /
``start_pipeline_run`` (async), which serialize the FULL ``Pipeline`` into the
work-order's ``invocation.json`` (NOT a registry name). So an inline run is
crash-recoverable IDENTICALLY to a registered one — the recovery scan
(``AgentRegistry._rewake_pipeline_runs``) re-creates the driver-session from
``invocation.json`` alone, with no registry lookup and no new recovery source.

**The static-analysis gate (IS-4, R6 §7.3 — the validation gate for
agent-GENERATED artifacts).** A generated pipeline is untrusted-by-shape: it
must be checked before it can spawn a driver-session. For the LINEAR subset the
gate is deliberately MINIMAL (the full cost-bound / dataflow / spawn-tree
analyzer belongs with the non-linear primitives, a later slice) — six checks,
all statically decidable over the parsed ``Pipeline`` + its ``SchemaRegistry``:

  1. **parse succeeds** — ``parse_pipeline_dsl`` raises ``PipelineParseError``
     for malformed DSL; the handler turns that into a clear tool error.
  2. **schema refs resolve** — every step ``schema:`` REF is registered in the
     parsed registry (i.e. a ``schema:`` document in the SAME definition string
     defines it). Catches a typo'd / undefined ref before the run.
  3. **tool names resolve** — every ``tool`` step name resolves to a registered
     tool (qualified-action routing, then a bare registry lookup — the SAME
     resolution :func:`_make_tool_dispatch` performs at run time).
  4. **capability ⊆ invoker** — already STRUCTURAL, no runtime re-check needed:
     the driver-session is spawned under the INVOKER's own identity
     (``_spawn_pipeline_driver_session``), and an ``agent`` step narrows
     RESTRICT-ONLY (``_build_agent_step_narrowing``), so a generated pipeline
     can never exceed the invoker's envelope by construction. The gate only
     DOCUMENTS this; check 6 closes the one hole it leaves.
  5. **S3 no nested launch** — a ``tool`` step must not itself launch a pipeline
     or delegate (nesting is ``call``-only; enforced structurally at dispatch
     via ``_PIPELINE_STEP_DENY_TOOLS``, validated statically here so a bad
     generated pipeline fails fast at the gate, not mid-run).
  6. **agent-step identity == invoker** (INLINE-ONLY, escalation prevention) —
     an ``agent`` step may only run under the invoker's own identity
     (``identity`` unset = inherit invoker, or explicitly the invoker's name).
     A generated pipeline naming ANOTHER agent's identity would run under that
     agent's (possibly larger) profile — a capability escalation, since check
     4's ⊆-invoker guarantee holds only for identity==invoker. Registered
     pipelines are exempt (a trusted registrant deliberately chose the
     identity); this check applies to inline definitions only.

A gate failure returns a clear tool error and spawns NOTHING (the checks run
before ``run_pipeline_attached`` / ``start_pipeline_run`` is called).

  - **Sync = async + an attached live view (IS-6).** ``run_pipeline`` no longer
    runs the executor inline on the caller's turn (that was IS-1, which meant a
    sync run could not crash-recover). It now spawns the SAME crash-recoverable
    ``PipelineExecutorDriver`` driver-session as ``run_pipeline_async`` and
    ATTACHES: ``reyn.runtime.session_api.run_pipeline_attached`` pumps the run on
    the caller's own task via ``MessageBus.request``, streams
    ``pipeline_step_started`` / ``pipeline_step_completed`` events (each carrying
    ``total_steps``, #2570) to the driver-session's ``EventLog`` (the emit+
    subscribe seam a live view / the TUI consumes), and reads the terminal marker
    back in-band — no redundant reply turn (``notify_reply=False``). A crash
    mid-attach degrades to async recovery: the recovery scan resumes the run and
    delivers to THIS caller's inbox.
    **TUI bridge marker (#2570)**: the ``pipeline_step_*`` events above land on
    the DRIVER-session's own ``EventLog`` — a session distinct from the
    human-attached caller the TUI actually watches. So both sync handlers pass
    ``tool``/``caller_events=ctx.events`` through to ``run_pipeline_attached``,
    which emits a ``pipeline_run_attached`` marker (``{tool, run_id, driver_sid,
    agent_name, pipeline_name}``) onto the CALLER's own ``EventLog`` right after
    the driver-session spawns — the signal a live view uses to bridge-subscribe
    to the driver_sid's events for the run's duration. The async handlers
    (``_handle_run_pipeline_async`` / ``_handle_run_pipeline_inline_async``) have
    no attached live viewer and never pass these — no marker.
    Ctrl-C stops the run cooperatively at the next step BOUNDARY, leaving a
    resumable R4 journal under a terminal ``cancelled`` marker. #2588: the
    Ctrl-C hits ``cancel_inflight`` on the ATTACHED CALLER session, not the
    spawned driver-session; ``run_pipeline_attached`` bridges it by registering
    the driver's ``request_cancel`` as a cancel-forward on the caller for the
    attached run's duration (``Session.register_cancel_forward``), so the
    caller's Ctrl-C reaches the driver's step-boundary ``cancel_check``.
  - **Real tool-step dispatch, not a stub.** A pipeline ``ToolStep``'s
    ``tool_dispatch`` is wired through the SAME routing seam
    ``invoke_action`` uses (``universal_dispatch.resolve_invoke_action`` +
    the unified ``ToolRegistry`` — see :func:`_make_tool_dispatch`), so a
    ``tool`` step actually executes a real capability (qualified action name
    OR bare registered tool name), not a caller-supplied fake.
  - **S3 cost-bound**: denied to pipeline-internal ``agent`` steps (an
    ``agent`` step is a leaf worker — nesting is ``call``-only, a later
    slice) — enforced structurally in
    ``reyn.runtime.session_api._build_agent_step_narrowing``, not here.

Dependencies the sync handler assembles for the attached driver-session launch
(the SAME set ``run_pipeline_async`` needs — a driver-session spawns under an
identity, anchors its work-order on a WAL, and replies to the caller):
  - ``agent_registry`` (spawn the driver-session under the invoker) from
    ``ctx.router_state.agent_registry``.
  - ``state_log`` (anchors ``invocation.json`` + the R4 recovery generations)
    from ``ctx.state_log`` — the SAME process-shared WAL every other
    recovery-aware tool threads.
  - ``host`` (the calling actor's ``agent_name`` + ``live_session_id`` = the
    reply address, so a crash-recovered run delivers back here) from
    ``ctx.router_state.host``.
  - ``tool_dispatch`` — see :func:`_make_tool_dispatch`.

NOTE (surfacing, IS-5): this tool is registered in the unified
``ToolRegistry`` (dispatch-completeness: routable via
``invoke_action``/``pipeline__run``, classified for the content-threat +
capability-floor guards) and IS surfaced to the live LLM — not via
``build_tools()`` (which is hand-assembled and strips direct tools once the
universal-catalog wrappers are on; PR-3b already shipped that default-on),
but via the same modern path every other universal-catalog wrapper uses: the
``pipeline`` resource category in ``tools/universal_catalog.py:
_enumerate_category`` lists each REGISTERED pipeline (name + description)
from ``ctx.router_state.pipeline_registry``, and the LLM launches a chosen
one through ``invoke_action(action="pipeline__run", args={name, input})``.
``Session`` (``runtime/session.py``) constructs + owns the production
``PipelineRegistry`` that backs this (empty until a later slice populates it
from disk / a parser); it is threaded through ``RouterHostAdapter`` onto
``RouterCallerState.pipeline_registry`` by
``RouterLoop._build_router_caller_state``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from reyn.core.pipeline.executor import (
    AgentStep,
    Pipeline,
    PipelineExecutionError,
    ToolStep,
)
from reyn.core.pipeline.registry import PipelineNotFoundError
from reyn.tools.descriptions import pipeline as _pipeline_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

if TYPE_CHECKING:
    from reyn.core.pipeline.schema import SchemaRegistry

# Relocated to reyn.tools.descriptions.pipeline (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_RUN_PIPELINE_DESCRIPTION = _pipeline_descriptions.run_pipeline.text

_RUN_PIPELINE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["name"].text,
        },
        "input": {
            "type": "object",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["input"].text,
        },
    },
    "required": ["name"],
}

# R6 S3 structural deny for pipeline TOOL steps (IS-2 sibling sweep of the
# agent-step ``_DELEGATION_DENY_TOOLS`` in ``runtime/session_api.py``): a
# ``ToolStep`` that dispatches a pipeline launch (sync or async) or a
# delegation would nest agentic work under a step, defeating the
# transitive-closure cost-bound approval a REGISTERED pipeline gets at launch
# time — nesting is ``call``-only. Checked on BOTH the raw step name and the
# post-``resolve_invoke_action`` target, so the qualified forms
# (``pipeline__run`` / ``multi_agent__delegate``) are covered too.
_PIPELINE_STEP_DENY_TOOLS: "frozenset[str]" = frozenset({
    "run_pipeline", "run_pipeline_async", "delegate_to_agent",
    # IS-4 sibling sweep: the inline launch verbs are the same escape hatch as
    # the registered ones — an inline pipeline is STILL non-grantable inside a
    # pipeline step (nesting is call-only). Kept in lock-step with the
    # ``_DELEGATION_DENY_TOOLS`` agent-step deny in ``runtime/session_api.py``.
    "run_pipeline_inline", "run_pipeline_inline_async",
})


def _make_tool_dispatch(ctx: ToolContext) -> "Callable[[str, dict], Any]":
    """Build the real ``tool_dispatch`` a pipeline ``ToolStep`` invokes through.

    Routes ``step.name`` through the SAME seam ``invoke_action`` uses
    (``universal_dispatch.resolve_invoke_action`` — see
    ``tools/universal_catalog.py:_handle_invoke_action``, the precedent this
    mirrors): a qualified action name (``file__read``) resolves to its target
    tool + shaped args; a name with no operation-rule route falls back to a
    direct bare-name lookup in the unified registry (so a pipeline can also
    name a tool directly, e.g. ``"web_search"``). Either way the target
    handler is invoked with ``ctx`` forwarded VERBATIM — same as
    ``invoke_action`` forwards it — so router_state callbacks (permission
    resolver, workspace, etc.) reach the target exactly as if the caller had
    invoked it directly. No stub, no op_runtime bridge: this IS the real
    tool-execution path.
    """

    async def _dispatch(name: str, resolved_args: "dict[str, Any]") -> Any:
        from reyn.tools import get_default_registry
        from reyn.tools.universal_dispatch import (
            UnknownActionError,
            resolve_invoke_action,
        )

        registry = get_default_registry()
        target_name = name
        target_args: "dict[str, Any]" = dict(resolved_args)
        try:
            resolved = resolve_invoke_action(name, resolved_args)
        except UnknownActionError:
            resolved = None
        if resolved is not None:
            target_name = resolved.target_tool_name
            target_args = dict(resolved.target_args)

        if name in _PIPELINE_STEP_DENY_TOOLS or target_name in _PIPELINE_STEP_DENY_TOOLS:
            raise PipelineExecutionError(
                f"pipeline tool step {name!r} is structurally denied (R6 S3): "
                "a step must not launch a pipeline or delegate — nesting is "
                "call-only, so the launch-time cost-bound approval stays a "
                "transitive closure."
            )

        target = registry.lookup(target_name)
        if target is None:
            raise PipelineExecutionError(
                f"pipeline tool step {name!r} does not resolve to a "
                f"registered tool (tried qualified-action routing, then a "
                f"bare lookup of {target_name!r})"
            )
        result = await target.handler(target_args, ctx)
        # FP-0056 PR-F1: tag the RESOLVED target tool name so _run_tool_step canonicalizes by invoked
        # identity (declaration born at the tool's registration seam), not result["kind"]. Stripped
        # before schema validation + ctx exposure in _run_tool_step.
        if isinstance(result, dict) and "_canonical_source" not in result:
            result = {**result, "_canonical_source": target_name}
        return result

    return _dispatch


async def _handle_run_pipeline(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Look up the registered pipeline, run it in an ATTACHED driver-session,
    return its final output inline. See the module docstring for the wiring.

    IS-6: reworked from IS-1's inline ``PipelineExecutor().run`` to spawn the
    SAME crash-recoverable driver-session as ``run_pipeline_async`` and ATTACH
    to it (``run_pipeline_attached`` — ``MessageBus.request`` pumps the run on
    this task, live ``pipeline_step_*`` events flow to the driver-session's
    EventLog, the terminal marker is read back in-band). Sync therefore inherits
    crash auto-resume: if the process dies mid-run the recovery scan resumes it
    and delivers to THIS caller's inbox (sync degrades to async-recovery)."""
    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "data": {"error": "name is required"}}

    raw_input = args.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        return {
            "status": "error",
            "data": {"error": "input must be an object (mapping), if given"},
        }

    rs = ctx.router_state
    pipeline_registry = rs.pipeline_registry if rs is not None else None
    if pipeline_registry is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "no PipelineRegistry available — run_pipeline requires "
                    "ctx.router_state.pipeline_registry to be populated"
                ),
            },
        }

    try:
        pipeline = pipeline_registry.get(name)
        schema_registry = pipeline_registry.get_schema_registry(name)
    except PipelineNotFoundError:
        return {
            "status": "error",
            "data": {"error": f"pipeline {name!r} is not registered"},
        }

    # IS-6: the attached driver-session needs the same wiring as the async path
    # (agent_registry to spawn under + host for the caller identity/reply sid +
    # a WAL to anchor the work-order/recovery files). A non-persistent context
    # has no crash-recoverable run — same contract as run_pipeline_async.
    agent_registry = rs.agent_registry if rs is not None else None
    host = rs.host if rs is not None else None
    if agent_registry is None or host is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline requires a fully-wired router context "
                    "(agent_registry + host on ctx.router_state) to spawn its "
                    "attached driver-session"
                ),
            },
        }
    state_log = ctx.state_log
    if state_log is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline requires WAL persistence (ctx.state_log) — the "
                    "attached run is a crash-recoverable driver-session"
                ),
            },
        }

    from reyn.runtime.session_api import run_pipeline_attached

    reply_sid = getattr(host, "live_session_id", None) or "main"
    try:
        outcome = await run_pipeline_attached(
            agent_registry,
            pipeline=pipeline,
            pipeline_name=name,
            input=dict(raw_input) if raw_input else None,
            reply_to_agent=host.agent_name,
            reply_to_sid=reply_sid,
            state_log=state_log,
            tool="run_pipeline",
            caller_events=ctx.events,
            schema_registry=schema_registry,
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    status = outcome["status"]
    if status == "failed":
        return {
            "status": "error",
            "data": {
                "error": f"pipeline {name!r} failed: {outcome.get('error')}",
                "run_id": outcome["run_id"],
            },
        }
    if status == "cancelled":
        return {
            "status": "cancelled",
            "data": {
                "run_id": outcome["run_id"],
                "error": outcome.get("error"),
            },
        }
    if status == "running_async":
        # The attached wait did not reach terminal within the bound; the run was
        # handed to detached completion + inbox delivery (never lost). ``kind`` marks it as an
        # async start so the canonical mapper keeps ``run_id`` (the completion-message handle).
        return {"status": "started",
                "data": {"kind": "run_pipeline_async", "run_id": outcome["run_id"]}}

    # #2425 案B: ``kind`` drives the canonical mapper — the sync result's ``output`` is the whole
    # thing the caller wants; ``run_id``/``named_stores`` are dropped from the LLM-visible side.
    return {
        "status": "ok",
        "data": {
            "kind": "run_pipeline",
            "run_id": outcome["run_id"],
            "output": outcome.get("output"),
            "named_stores": outcome.get("named_stores"),
        },
    }


from reyn.core.offload.canonical import (  # noqa: E402
    run_pipeline_async_to_canonical,
    run_pipeline_to_canonical,
)

RUN_PIPELINE = ToolDefinition(
    canonical=run_pipeline_to_canonical,
    name="run_pipeline",
    description=_RUN_PIPELINE_DESCRIPTION,
    parameters=_RUN_PIPELINE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline,
    category="io",
    purity="side_effect",
)


# Relocated to reyn.tools.descriptions.pipeline (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_RUN_PIPELINE_ASYNC_DESCRIPTION = _pipeline_descriptions.run_pipeline_async.text


async def _handle_run_pipeline_async(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """IS-2: resolve the registered pipeline, hand it to
    ``runtime.session_api.start_pipeline_run`` (spawn driver-session → persist
    ``invocation.json`` BEFORE step 0 → inject ``PipelineExecutorDriver`` →
    nudge), and return ``{status: started, run_id}`` without waiting. The
    result routes back to THIS caller's (agent, live sid) as a
    ``pipeline_result`` inbox message. Requires a WAL (``ctx.state_log``) —
    the async architecture IS the crash-recovery architecture, so a
    non-persistent context has no async launch."""
    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "data": {"error": "name is required"}}
    raw_input = args.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        return {
            "status": "error",
            "data": {"error": "input must be an object (mapping), if given"},
        }

    rs = ctx.router_state
    pipeline_registry = rs.pipeline_registry if rs is not None else None
    agent_registry = rs.agent_registry if rs is not None else None
    host = rs.host if rs is not None else None
    if pipeline_registry is None or agent_registry is None or host is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline_async requires a fully-wired router context "
                    "(pipeline_registry + agent_registry + host on "
                    "ctx.router_state)"
                ),
            },
        }
    state_log = ctx.state_log
    if state_log is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline_async requires WAL persistence "
                    "(ctx.state_log) — the async run is crash-recoverable by "
                    "construction; use run_pipeline for a non-persistent sync run"
                ),
            },
        }

    try:
        pipeline = pipeline_registry.get(name)
        schema_registry = pipeline_registry.get_schema_registry(name)
    except PipelineNotFoundError:
        return {
            "status": "error",
            "data": {"error": f"pipeline {name!r} is not registered"},
        }

    from reyn.runtime.session_api import start_pipeline_run

    reply_sid = getattr(host, "live_session_id", None) or "main"
    try:
        run_id = await start_pipeline_run(
            agent_registry,
            pipeline=pipeline,
            pipeline_name=name,
            input=dict(raw_input) if raw_input else None,
            reply_to_agent=host.agent_name,
            reply_to_sid=reply_sid,
            state_log=state_log,
            schema_registry=schema_registry,
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    # #2425 案B: ``kind`` drives the canonical mapper — the async result KEEPS ``run_id`` (the handle
    # the caller matches against the later [pipeline] completion message).
    return {"status": "started", "data": {"kind": "run_pipeline_async", "run_id": run_id}}


RUN_PIPELINE_ASYNC = ToolDefinition(
    canonical=run_pipeline_async_to_canonical,
    name="run_pipeline_async",
    description=_RUN_PIPELINE_ASYNC_DESCRIPTION,
    parameters=_RUN_PIPELINE_PARAMETERS,  # same surface: name + optional input
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline_async,
    category="io",
    purity="side_effect",
)

# ── IS-4: ad-hoc INLINE launch (agent-GENERATED DSL + static-analysis gate) ──

_RUN_PIPELINE_INLINE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "definition": {
            "type": "string",
            "description": _pipeline_descriptions.PARAMS["run_pipeline_inline"]["definition"].text,
        },
        "input": {
            "type": "object",
            "description": _pipeline_descriptions.PARAMS["run_pipeline_inline"]["input"].text,
        },
    },
    "required": ["definition"],
}

# Relocated to reyn.tools.descriptions.pipeline (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_RUN_PIPELINE_INLINE_DESCRIPTION = _pipeline_descriptions.run_pipeline_inline.text

_RUN_PIPELINE_INLINE_ASYNC_DESCRIPTION = (
    _pipeline_descriptions.run_pipeline_inline_async.text
)


def _static_analysis_gate(
    pipeline: "Pipeline",
    schema_registry: "SchemaRegistry",
    *,
    invoker_agent: str,
) -> "str | None":
    """The IS-4 minimal static-analysis gate for an agent-GENERATED pipeline.

    Runs the six R6 §7.3 checks (see the module docstring) over the already-
    parsed ``pipeline`` + its ``schema_registry``, returning a clear error
    string on the FIRST failing check or ``None`` when all pass. Check 1
    (parse) is handled by the caller (``parse_pipeline_dsl`` raises before this
    is reached); check 4 (capability ⊆ invoker) is structural and needs no
    runtime probe here (documented in the module docstring). This function is
    PURE — it inspects the parsed artifact and the tool registry, spawns
    nothing, so the caller can run it strictly before any driver-session launch.
    """
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        resolve_invoke_action,
    )

    registry = get_default_registry()
    for i, step in enumerate(pipeline.steps):
        # Check 2: schema REF resolves in the parsed (inline) registry.
        schema = getattr(step, "schema", None)
        if schema is not None and not schema_registry.has(schema):
            return (
                f"step {i}: schema ref {schema!r} does not resolve — no "
                "'schema:' document in the definition defines it"
            )
        # Checks 3 + 5: tool-step name resolution + S3 nested-launch deny. Mirror
        # the run-time resolution ``_make_tool_dispatch`` performs (qualified
        # action routing first, then a bare registry lookup) so the static verdict
        # matches what would actually dispatch.
        if isinstance(step, ToolStep):
            name = step.name
            target_name = name
            try:
                resolved = resolve_invoke_action(name, {})
            except UnknownActionError:
                resolved = None
            if resolved is not None:
                target_name = resolved.target_tool_name
            # Check 5 (S3): reject BEFORE the registry lookup — a launch/delegate
            # verb IS a registered tool, so lookup would pass; the deny must win.
            if name in _PIPELINE_STEP_DENY_TOOLS or target_name in _PIPELINE_STEP_DENY_TOOLS:
                return (
                    f"step {i}: tool {name!r} is structurally denied (R6 S3) — an "
                    "inline pipeline step must not launch a pipeline or delegate "
                    "(nesting is call-only)"
                )
            # Check 3: the tool name must resolve to a registered tool.
            if registry.lookup(target_name) is None:
                return (
                    f"step {i}: tool {name!r} does not resolve to a registered "
                    f"tool (tried qualified-action routing, then a bare lookup of "
                    f"{target_name!r})"
                )
        # Check 6 (INLINE-ONLY): an agent step may only run under the invoker's
        # own identity — a non-invoker identity is a capability escalation.
        if isinstance(step, AgentStep):
            if step.identity is not None and step.identity != invoker_agent:
                return (
                    f"step {i}: agent step identity {step.identity!r} is not the "
                    f"invoker {invoker_agent!r} — an inline pipeline may only run "
                    "agent steps under the invoker's own identity (capability "
                    "escalation prevention, R6 constraint b); omit 'identity' to "
                    "inherit the invoker, or name the invoker explicitly"
                )
    return None


def _prepare_inline_launch(
    args: Mapping[str, Any], ctx: ToolContext,
) -> "tuple[dict | None, tuple | None]":
    """Shared inline prelude for both inline verbs: validate args, require a
    fully-wired persistent context, parse the ``definition`` DSL string into a
    ``Pipeline`` (with a FRESH per-call ``SchemaRegistry`` populated from the
    definition's own inline ``schema:`` docs), and run the static-analysis gate.

    Returns ``(error_result, None)`` on any validation / parse / gate failure
    (the caller returns ``error_result`` verbatim, having spawned NOTHING), or
    ``(None, (pipeline, schema_registry, agent_registry, host, state_log,
    raw_input))`` when the definition is clean and ready to launch.

    ``schema_registry`` is NOT threaded onto ``ctx.router_state`` or any
    persistent registry — an inline definition is self-contained (its schemas
    live only in the DSL string), matching the "no persistent inline
    registry" design decision. It IS (#2572) threaded to the launch call
    (``run_pipeline_attached``/``start_pipeline_run``), which persists it onto
    the work-order (``schema_defs``) so the driver-session's ``verify:
    schema`` steps are actually enforced — the per-call registry was
    previously parsed and then discarded, so an inline ``verify: schema`` step
    crashed the driver with "no schema_registry" instead of validating."""
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaError, SchemaRegistry

    definition = args.get("definition")
    if not isinstance(definition, str) or not definition.strip():
        return {
            "status": "error",
            "data": {"error": "definition is required (a pipeline DSL string)"},
        }, None

    raw_input = args.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        return {
            "status": "error",
            "data": {"error": "input must be an object (mapping), if given"},
        }, None

    rs = ctx.router_state
    agent_registry = rs.agent_registry if rs is not None else None
    host = rs.host if rs is not None else None
    if agent_registry is None or host is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline_inline requires a fully-wired router context "
                    "(agent_registry + host on ctx.router_state) to spawn its "
                    "driver-session"
                ),
            },
        }, None
    state_log = ctx.state_log
    if state_log is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline_inline requires WAL persistence (ctx.state_log) "
                    "— an inline run is a crash-recoverable driver-session"
                ),
            },
        }, None

    # Check 1 (parse): a fresh registry so an inline definition is self-contained
    # (its schemas never leak across calls). Malformed DSL / expression / a
    # schema-shape error surfaces as a clear gate error, nothing spawned.
    schema_registry = SchemaRegistry()
    try:
        pipeline = parse_pipeline_dsl(definition, schema_registry)
    except (PipelineParseError, SchemaError) as exc:
        return {
            "status": "error",
            "data": {"error": f"inline pipeline definition is invalid: {exc}"},
        }, None

    # Checks 2/3/5/6 (schema refs, tool names, S3, agent identity).
    gate_error = _static_analysis_gate(
        pipeline, schema_registry, invoker_agent=host.agent_name,
    )
    if gate_error is not None:
        return {
            "status": "error",
            "data": {"error": f"inline pipeline rejected by static gate: {gate_error}"},
        }, None

    return None, (pipeline, schema_registry, agent_registry, host, state_log, raw_input)


async def _handle_run_pipeline_inline(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """IS-4 sync INLINE launch: parse + statically gate the agent-generated
    ``definition``, then run it in the SAME attached driver-session
    ``run_pipeline`` uses (``run_pipeline_attached``) — so the inline run is
    crash-recoverable and returns its output inline, exactly like a registered
    run. A gate failure returns a clear error and spawns nothing."""
    error_result, launch = _prepare_inline_launch(args, ctx)
    if error_result is not None:
        return error_result
    pipeline, schema_registry, agent_registry, host, state_log, raw_input = launch

    from reyn.runtime.session_api import run_pipeline_attached

    reply_sid = getattr(host, "live_session_id", None) or "main"
    try:
        outcome = await run_pipeline_attached(
            agent_registry,
            pipeline=pipeline,
            pipeline_name="inline",
            input=dict(raw_input) if raw_input else None,
            reply_to_agent=host.agent_name,
            reply_to_sid=reply_sid,
            state_log=state_log,
            tool="run_pipeline_inline",
            caller_events=ctx.events,
            schema_registry=schema_registry,
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    status = outcome["status"]
    if status == "failed":
        return {
            "status": "error",
            "data": {
                "error": f"inline pipeline failed: {outcome.get('error')}",
                "run_id": outcome["run_id"],
            },
        }
    if status == "cancelled":
        return {
            "status": "cancelled",
            "data": {"run_id": outcome["run_id"], "error": outcome.get("error")},
        }
    if status == "running_async":
        return {"status": "started",
                "data": {"kind": "run_pipeline_async", "run_id": outcome["run_id"]}}

    # #2425 案B: ``kind`` drives the canonical mapper (sync output → text/structured, run_id dropped).
    return {
        "status": "ok",
        "data": {
            "kind": "run_pipeline",
            "run_id": outcome["run_id"],
            "output": outcome.get("output"),
            "named_stores": outcome.get("named_stores"),
        },
    }


async def _handle_run_pipeline_inline_async(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """IS-4 async INLINE launch: parse + statically gate the agent-generated
    ``definition``, then hand it to the SAME background driver-session
    ``run_pipeline_async`` uses (``start_pipeline_run``) and return
    ``{status: started, run_id}`` immediately; the result arrives later as a
    ``pipeline_result`` inbox message. A gate failure returns a clear error and
    spawns nothing."""
    error_result, launch = _prepare_inline_launch(args, ctx)
    if error_result is not None:
        return error_result
    pipeline, schema_registry, agent_registry, host, state_log, raw_input = launch

    from reyn.runtime.session_api import start_pipeline_run

    reply_sid = getattr(host, "live_session_id", None) or "main"
    try:
        run_id = await start_pipeline_run(
            agent_registry,
            pipeline=pipeline,
            pipeline_name="inline",
            input=dict(raw_input) if raw_input else None,
            reply_to_agent=host.agent_name,
            reply_to_sid=reply_sid,
            state_log=state_log,
            schema_registry=schema_registry,
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    # #2425 案B: ``kind`` drives the canonical mapper — the async result KEEPS ``run_id`` (the handle
    # the caller matches against the later [pipeline] completion message).
    return {"status": "started", "data": {"kind": "run_pipeline_async", "run_id": run_id}}


RUN_PIPELINE_INLINE = ToolDefinition(
    canonical=run_pipeline_to_canonical,
    name="run_pipeline_inline",
    description=_RUN_PIPELINE_INLINE_DESCRIPTION,
    parameters=_RUN_PIPELINE_INLINE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline_inline,
    category="io",
    purity="side_effect",
)


RUN_PIPELINE_INLINE_ASYNC = ToolDefinition(
    canonical=run_pipeline_async_to_canonical,
    name="run_pipeline_inline_async",
    description=_RUN_PIPELINE_INLINE_ASYNC_DESCRIPTION,
    parameters=_RUN_PIPELINE_INLINE_PARAMETERS,  # same surface: definition + input
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline_inline_async,
    category="io",
    purity="side_effect",
)

__all__ = [
    "RUN_PIPELINE",
    "RUN_PIPELINE_ASYNC",
    "RUN_PIPELINE_INLINE",
    "RUN_PIPELINE_INLINE_ASYNC",
]
