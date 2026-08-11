"""Pipeline launch router tools — REGISTERED + ad-hoc INLINE, sync + async.

Per ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6: an agent
launches a pipeline and collects its result. Proposal 0067 P7 (#3978) unified
what used to be four separate launch verbs
(``run_pipeline``/``run_pipeline_async``/``run_pipeline_inline``/
``run_pipeline_inline_async``, 0 aliases kept) into ONE — ``run_pipeline`` —
distinguished by two params instead of by name:

  - ``name=`` XOR ``definition=`` (exactly one, validated explicitly — never
    inferred from which one happens to be present) selects REGISTERED (a
    pipeline pre-built via :class:`reyn.core.pipeline.registry.PipelineRegistry`,
    looked up by name) vs. ad-hoc INLINE (IS-4: a DSL STRING the agent
    GENERATED at runtime, Appendix B grammar). The inline definition is parsed
    (``reyn.core.pipeline.parser.parse_pipeline_dsl``, IS-3 — including any
    inline ``schema:`` documents in the same string, into a fresh per-call
    :class:`~reyn.core.pipeline.schema.SchemaRegistry`) into a ``Pipeline``,
    which then feeds the SAME downstream a registered launch uses. The inline
    path SKIPS the registry entirely — the only extra machinery over the
    registered path is the parse ENTRY and a **static-analysis gate** (see
    :func:`_static_analysis_gate`) that runs BEFORE anything is spawned.
  - ``collect="attached"`` (default) vs. ``collect="async"`` selects
    sync-attached vs. fire-and-forget, orthogonal to the ``name=``/
    ``definition=`` choice above. ``on_settle=`` (P4's vocabulary —
    ``"deliver"`` default | ``"<pipeline name>"`` | ``"drop"``) is accepted
    but IGNORED for ``collect="attached"`` (ADR-0040 D4: attached creates no
    settle handle at all) and threaded through for ``collect="async"``.

This module hosts that single unified verb plus the tool-step dispatch it
shares with every driver-session:

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
  4. **capability ⊆ invoker** — partly structural, and the rest is enforced at
     run time. The driver-session is spawned under the INVOKER's own identity
     (``_spawn_pipeline_driver_session``), which carries the IDENTITY-keyed
     layers of the envelope for free: the agent's own ``permissions``
     declaration, its topology ``capability_profile`` bindings, and the #2081
     ``_delegate`` floor all resolve from the agent NAME, so a same-identity
     child re-derives them unchanged. An ``agent`` step additionally narrows
     RESTRICT-ONLY (``_build_agent_step_narrowing``) — which #3553 had to make
     true rather than merely restate: that function used to build the worker's
     WHOLE narrowing from the step's own ``capabilities`` plus the delegation
     deny, so a step declaring no ``capabilities`` handed its worker no allow
     restriction at all, losing the invoker's SID-keyed one for the same reason
     the driver-session did. It now composes the invoker's mapping in
     (``capability_profile.compose_narrowing_mappings``: denies union, allows
     intersect, an absent allow key is ⊤).
     ⚠️ This check used to claim the whole envelope followed from identity ("⊆
     by construction, no runtime re-check needed"). It does not: the #2103-S1a
     per-session narrowing is keyed by SID, not by identity, so a fresh
     driver-session resolved it to nothing — and this module's own
     :func:`_make_tool_dispatch` runs OUTSIDE any ``RouterLoop``, so neither of
     the RouterLoop TOOL-axis gates was in the path either. Measured on the
     unfixed code (#3546): a session narrowed with ``tool_deny: [X]`` ran a
     pipeline whose step invoked ``X`` and ``X``'s real side effect happened.
     Both halves are now closed — the spawn passes ``narrowing=`` (the seam
     where the envelope is born) and the tool-step dispatch consults the
     session's live contextual through the shared
     ``effective.tool_contextually_denied`` predicate. Check 6 closes the
     separate identity hole.
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

  - **``collect="attached"`` = ``collect="async"`` + an attached live view
    (IS-6).** The attached path no longer runs the executor inline on the
    caller's turn (that was IS-1, which meant it could not crash-recover). It
    now spawns the SAME crash-recoverable ``PipelineExecutorDriver``
    driver-session as ``collect="async"`` and
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
    to the driver_sid's events for the run's duration. The ``collect="async"``
    branch of the shared handler has no attached live viewer and never passes
    these — no marker.
    Ctrl-C stops the run cooperatively at the next step BOUNDARY, leaving a
    resumable R4 journal under a terminal ``cancelled`` marker. #2588: the
    Ctrl-C hits ``cancel_inflight`` on the ATTACHED CALLER session, not the
    spawned driver-session; ``run_pipeline_attached`` bridges it by registering
    the driver's ``request_cancel`` as a cancel-forward on the caller for the
    attached run's duration (``Session.register_cancel_forward``), so the
    caller's Ctrl-C reaches the driver's step-boundary ``cancel_check``.
  - **Real tool-step dispatch, not a stub.** A pipeline ``ToolStep``'s
    ``tool_dispatch`` resolves ``step.name`` in the unified ``ToolRegistry``
    and runs that tool's own handler (see :func:`_make_tool_dispatch`), so a
    ``tool`` step actually executes a real capability, not a caller-supplied
    fake.
  - **S3 cost-bound**: denied to pipeline-internal ``agent`` steps (an
    ``agent`` step is a leaf worker — nesting is ``call``-only, a later
    slice) — enforced structurally in
    ``reyn.runtime.session_api._build_agent_step_narrowing``, not here.

Dependencies the ``collect="attached"`` branch assembles for the driver-session
launch (the SAME set the ``collect="async"`` branch needs — a driver-session
spawns under an identity, anchors its work-order on a WAL, and replies to the
caller):
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
``invoke_action``/``run_pipeline``, classified for the content-threat +
capability-floor guards) and IS surfaced to the live LLM — not via
``build_tools()`` (which is hand-assembled and strips direct tools once the
universal-catalog wrappers are on; PR-3b already shipped that default-on),
but via the same modern path every other universal-catalog wrapper uses.

#3026: discovery of a REGISTERED pipeline is now the ``pipeline_list`` verb
(``pipeline_list`` below), which returns each registered pipeline's name +
description from ``ctx.router_state.pipeline_registry``; the LLM launches a
chosen one through ``invoke_action(action="run_pipeline", args={name,
input})``. Previously ``_enumerate_category`` emitted one
action per REGISTERED pipeline — so the LLM's ``tools=``
payload grew with the operator's pipelines, and the per-pipeline action was
in any case a pure currying of ``run_pipeline`` (it forwarded ``input``
and curried ``name``, reaching ``run_pipeline`` with identical effective
args). Collapsing it cost no capability; the one thing it did uniquely —
NAMING the registered pipelines — is what ``pipeline_list`` now does in a
constant single tool.
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
        "definition": {
            "type": "string",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["definition"].text,
        },
        "input": {
            "type": "object",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["input"].text,
        },
        "collect": {
            "type": "string",
            "enum": ["attached", "async"],
            "default": "attached",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["collect"].text,
        },
        "on_settle": {
            "type": "string",
            "description": _pipeline_descriptions.PARAMS["run_pipeline"]["on_settle"].text,
        },
    },
    # Proposal 0067 P7 (#3978): no ``required`` list — exactly one of `name`/
    # `definition` is required, validated explicitly in the handler (never
    # inferred from which one happens to be present, per architect ruling:
    # both-given or neither-given must both fail loud, not silently pick).
}

# R6 S3 structural deny for pipeline TOOL steps (IS-2 sibling sweep of the
# agent-step ``_DELEGATION_DENY_TOOLS`` in ``runtime/session_api.py``): a
# ``ToolStep`` that dispatches a pipeline launch (sync or async) or a
# delegation would nest agentic work under a step, defeating the
# transitive-closure cost-bound approval a REGISTERED pipeline gets at launch
# time — nesting is ``call``-only. One name per tool (#3429), so checking the
# step name is the whole check: there is no second spelling that could reach a
# denied tool past this set.
_PIPELINE_STEP_DENY_TOOLS: "frozenset[str]" = frozenset({
    # Proposal 0067 P7 (#3978): run_pipeline_async / run_pipeline_inline /
    # run_pipeline_inline_async retired (4 names -> 1, architect ruling: 0
    # aliases). The single surviving name still needs the deny -- an inline
    # OR registered pipeline is STILL non-grantable inside a pipeline step
    # (nesting is call-only) regardless of collect=/definition= choice.
    # Kept in lock-step with ``_DELEGATION_DENY_TOOLS`` in
    # ``runtime/session_api.py`` — see ``test_pipeline_step_deny_sets_are_equal``
    # (the equality gate architect required, since P6/P7 both edit these two
    # sets and a drift here is silent: neither side's own test can see the
    # other set at all). Proposal 0067 P6 (#3978) retired the other former
    # member, ``delegate_to_agent`` — see ``_DELEGATION_DENY_TOOLS``'s own
    # comment for why nothing replaces it here.
    "run_pipeline",
    # #4244 (#4240's own split-out remainder, architect ruling): a confused
    # deputy — the LLM authors a pipeline containing a `tool: hooks_add`
    # step via the LLM-visible pipeline_install_local/_source tools; the
    # step's WRITE happens whenever a wider-authority principal later runs
    # that pipeline. `run_pipeline` (LLM-triggered) threads session_state_dir
    # correctly (#4241) and is safe; `reyn pipe run` (operator-triggered,
    # pipe.py's own session-less ToolContext) does not, and would write the
    # LLM-authored hook into the shared GLOBAL .reyn/config/hooks.yaml —
    # the exact "hooks is reactive, someone else's registration affects
    # you" shape #4215① exists to close, just with the roles of author and
    # trigger reversed. The operator's own direct edit of
    # .reyn/config/hooks.yaml remains open (self-evidently self-authored);
    # only the LLM-authored INDIRECT path through a pipeline step is denied.
    "hooks_add",
})

#: #4244: per-tool reason for a ``_PIPELINE_STEP_DENY_TOOLS`` hit — the set
#: now holds two semantically DIFFERENT denials (R6 S3 nesting for
#: ``run_pipeline``; confused-deputy for ``hooks_add``), so a single
#: hardcoded "nesting"/"launch a pipeline or delegate" message would be
#: actively wrong for the second. Falls back to the original nesting
#: wording for any future entry that doesn't register its own reason here
#: — every CURRENT member has one, so the fallback is only a safety net.
_PIPELINE_STEP_DENY_REASONS: "dict[str, str]" = {
    "run_pipeline": (
        "a step must not launch a pipeline or delegate — nesting is "
        "call-only, so the launch-time cost-bound approval stays a "
        "transitive closure"
    ),
    "hooks_add": (
        "a pipeline step must not self-write hooks — the step's author "
        "(possibly an LLM, via pipeline_install_local/_source) and the "
        "principal who eventually runs the pipeline (possibly an operator, "
        "via reyn pipe run) can differ, and reyn pipe run's session-less "
        "context would write the step-authored hook into the shared "
        "GLOBAL hooks.yaml rather than a session-local one (#4244)"
    ),
}


def _pipeline_step_deny_reason(name: str) -> str:
    """The human-readable reason *name* is in ``_PIPELINE_STEP_DENY_TOOLS`` —
    see :data:`_PIPELINE_STEP_DENY_REASONS`."""
    return _PIPELINE_STEP_DENY_REASONS.get(
        name,
        "a step must not launch a pipeline or delegate — nesting is "
        "call-only, so the launch-time cost-bound approval stays a "
        "transitive closure",
    )


def _make_tool_dispatch(
    ctx: ToolContext, *, contextual_permission: "object | None" = None,
) -> "Callable[[str, dict], Any]":
    """Build the real ``tool_dispatch`` a pipeline ``ToolStep`` invokes through.

    ``step.name`` is looked up directly in the unified registry and its handler
    invoked with ``ctx`` forwarded VERBATIM — same as ``invoke_action`` forwards
    it — so router_state callbacks (permission resolver, workspace, etc.) reach
    the tool exactly as if the caller had invoked it directly. No stub, no
    op_runtime bridge: this IS the real tool-execution path.

    #3546: ``contextual_permission`` is the running session's live
    ``ContextualPermission`` (``Session.contextual_permission``), consulted through
    the SAME shared predicate every other TOOL-axis site uses
    (``effective.tool_contextually_denied``). This path is the one tool-dispatch
    seam that does NOT run inside a ``RouterLoop`` — a pipeline driver-session
    runs ``PipelineExecutorDriver``, so neither the RouterLoop advertisement
    filter nor its ``_excluded_result`` call-time gate is in the path. Without
    this the narrowing a driver-session is born with (``session_api.
    _spawn_pipeline_driver_session``) would be persisted and never read on the
    surface that actually executes capabilities. ``None`` (the default, and what
    the ``reyn pipe`` CLI passes — an operator-direct run with no session
    envelope) leaves the dispatch byte-identical to pre-#3546.

    #3429: this used to try ``universal_dispatch.resolve_invoke_action`` first,
    so a step could name a tool by its second, ``<category>__<verb>`` spelling
    (``tool: file__read``) or by an author-time resource form (``tool:
    mcp__echo__ping``, ``tool: pipeline__greet``) that curried the resource id
    out of ``args``. Those spellings are gone: a step names the flat tool and
    passes the resource id as an ordinary argument
    (``mcp_call_tool{tool, tool_args}``, ``run_pipeline{name}``) — the shape the
    enumerated verbs already used, and the shape the two remaining call sites of
    that resolution had to special-case around.
    """

    async def _dispatch(name: str, resolved_args: "dict[str, Any]") -> Any:
        from reyn.tools import get_default_registry

        registry = get_default_registry()
        target_args: "dict[str, Any]" = dict(resolved_args)

        if name in _PIPELINE_STEP_DENY_TOOLS:
            raise PipelineExecutionError(
                f"pipeline tool step {name!r} is structurally denied: "
                f"{_pipeline_step_deny_reason(name)}."
            )

        # #3546: the TOOL-axis contextual gate, on the one dispatch seam that runs
        # outside a RouterLoop. Same predicate + same deny text as every other site.
        if contextual_permission is not None:
            from typing import cast

            from reyn.security.permissions.effective import (
                contextual_deny_message,
                tool_contextually_denied,
            )

            _ctx_perm = cast("Any", contextual_permission)
            if tool_contextually_denied(_ctx_perm, name):
                raise PipelineExecutionError(
                    contextual_deny_message("tool", name, _ctx_perm)
                )

        target = registry.lookup(name)
        if target is None:
            raise PipelineExecutionError(
                f"pipeline tool step {name!r} does not resolve to a "
                f"registered tool"
            )
        target_name = name
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
    """Proposal 0067 P7 (#3978): the unified pipeline launch verb — replaces
    ``run_pipeline`` / ``run_pipeline_async`` / ``run_pipeline_inline`` /
    ``run_pipeline_inline_async`` (4 names -> 1, retired with 0 aliases,
    architect ruling). See the module docstring for the shared wiring
    (static-analysis gate for ``definition``, the driver-session launch, the
    S3 nesting deny).

    Exactly one of ``name`` (a REGISTERED pipeline) or ``definition`` (an
    ad-hoc agent-GENERATED DSL string) selects the pipeline — validated
    explicitly (both given or neither given is an error; the handler never
    infers which one the caller meant from which happens to be present).
    ``collect`` (default ``"attached"``) selects sync-attached vs
    fire-and-forget ``"async"``. ``on_settle`` (P4's vocabulary: ``"deliver"``
    | ``"<pipeline name>"`` | ``"drop"``) is threaded through for
    ``collect="async"`` only — ADR-0040 D4 already established the attached
    path creates no settle handle at all, so it is accepted but ignored
    there (never silently rejected — a caller passing it for an attached run
    made a real request the tool just has nothing to do with, not a mistake
    to bounce)."""
    name = args.get("name")
    definition = args.get("definition")
    has_name = isinstance(name, str) and bool(name.strip())
    has_definition = isinstance(definition, str) and bool(definition.strip())
    if has_name == has_definition:
        return {
            "status": "error",
            "data": {
                "error": (
                    "exactly one of 'name' (a registered pipeline) or "
                    "'definition' (an ad-hoc DSL string) is required"
                ),
            },
        }

    collect = args.get("collect") or "attached"
    if collect not in ("attached", "async"):
        return {
            "status": "error",
            "data": {
                "error": f"collect must be 'attached' or 'async', got {collect!r}",
            },
        }
    on_settle = str(args.get("on_settle") or "deliver")

    raw_input = args.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        return {
            "status": "error",
            "data": {"error": "input must be an object (mapping), if given"},
        }

    rs = ctx.router_state

    # Validation order is deliberately branch-dependent, preserving each of
    # the two pre-P7 handlers' own order exactly (both are covered by
    # existing tests that pin the specific error message a given missing
    # piece of wiring produces):
    #   - registered (``name=``): pipeline_registry / name-lookup errors
    #     surface BEFORE the agent_registry/host/state_log check (the old
    #     ``_handle_run_pipeline``'s order — resolving *what* to run doesn't
    #     need a spawn-capable context).
    #   - inline (``definition=``): the agent_registry/host/state_log check
    #     surfaces BEFORE parse+gate (the old ``_prepare_inline_launch``'s
    #     order — an inline definition's static gate needs the invoker's
    #     identity, which comes from ``host``).
    if has_name:
        # Narrows for mypy: ``has_name`` is a derived bool, not a type guard on
        # ``name`` itself — the ``isinstance`` check above already proved this
        # at runtime, this just states it for the type checker too.
        assert isinstance(name, str)
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
        pipeline_name = name

    agent_registry = rs.agent_registry if rs is not None else None
    host = rs.host if rs is not None else None
    if agent_registry is None or host is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline requires a fully-wired router context "
                    "(agent_registry + host on ctx.router_state) to spawn its "
                    "driver-session"
                ),
            },
        }
    state_log = ctx.state_log
    if state_log is None:
        return {
            "status": "error",
            "data": {
                "error": (
                    "run_pipeline requires WAL persistence (ctx.state_log) — "
                    "every launch is a crash-recoverable driver-session"
                ),
            },
        }

    if not has_name:
        # Same narrowing as the ``has_name`` branch above, for ``definition``.
        assert isinstance(definition, str)
        error_result, launch = _prepare_inline_pipeline(
            definition, invoker_agent=host.agent_name,
        )
        if error_result is not None:
            return error_result
        pipeline, schema_registry = launch
        pipeline_name = "inline"

    reply_sid = getattr(host, "live_session_id", None) or "main"

    if collect == "attached":
        from reyn.runtime.session_api import run_pipeline_attached

        try:
            outcome = await run_pipeline_attached(
                agent_registry,
                pipeline=pipeline,
                pipeline_name=pipeline_name,
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
            # #2649: the standard dispatch-error shape ({status:error, error:{kind, message}})
            # so ``router_loop.feedback()``'s error path renders ``Error (<kind>): <message>``
            # like every other tool, instead of falling through to the generic canonical
            # fallback (top-level ``error`` used to be a nested string, not a dict — see
            # ``dispatcher.py``'s ``_error`` for the vocabulary this ``kind`` follows). No data
            # lost: ``run_id`` stays reachable for the LLM, folded into the message text (the
            # dispatch-error envelope carries no third field alongside ``kind``/``message``).
            return {
                "status": "error",
                "error": {
                    "kind": "pipeline_failed",
                    "message": (
                        f"pipeline {pipeline_name!r} failed (run_id: {outcome['run_id']}): "
                        f"{outcome.get('error')}"
                    ),
                },
            }
        if status == "cancelled":
            # #2649: distinguished from ``failed`` by ``kind`` (not by a separate top-level
            # ``status`` value anymore) — same standard shape, same reasoning as above.
            return {
                "status": "error",
                "error": {
                    "kind": "pipeline_cancelled",
                    "message": (
                        f"pipeline {pipeline_name!r} cancelled (run_id: {outcome['run_id']}): "
                        f"{outcome.get('error')}"
                    ),
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

    # collect == "async"
    from reyn.runtime.session_api import start_pipeline_run

    try:
        run_id = await start_pipeline_run(
            agent_registry,
            pipeline=pipeline,
            pipeline_name=pipeline_name,
            input=dict(raw_input) if raw_input else None,
            reply_to_agent=host.agent_name,
            reply_to_sid=reply_sid,
            state_log=state_log,
            schema_registry=schema_registry,
            on_settle=on_settle,
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    # #2425 案B: ``kind`` drives the canonical mapper — the async result KEEPS ``run_id`` (the handle
    # the caller matches against the later [pipeline] completion message).
    return {"status": "started", "data": {"kind": "run_pipeline_async", "run_id": run_id}}


from reyn.core.offload.canonical import (  # noqa: E402
    pipeline_list_to_canonical,
    run_pipeline_unified_to_canonical,
)

# ── pipeline_list (#3026) ───────────────────────────────────────────────────

_PIPELINE_LIST_DESCRIPTION = _pipeline_descriptions.pipeline_list.text

# No parameters: the result is already scoped to this session's registered set,
# and the caller's whole problem is that it does not yet know what exists — so
# there is nothing for it to filter by.
_PIPELINE_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


async def _handle_pipeline_list(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Return the registered pipelines: ``{pipelines: [{name, description}, ...]}``.

    Reads the SAME ``ctx.router_state.pipeline_registry`` the catalog used to
    enumerate a per-pipeline action from, so this verb and ``run_pipeline``
    cannot disagree about which pipelines exist.

    A None registry — a narrow test host, or a host that does not support
    run_pipeline — yields an empty list rather than an error: "no pipelines are
    registered" is the truthful answer in exactly that case, and it matches how
    the catalog enumeration has always degraded.

    ``name`` is the load-bearing field: it is what the caller passes back as
    ``run_pipeline(name=...)``.
    """
    rs = getattr(ctx, "router_state", None)
    registry = getattr(rs, "pipeline_registry", None) if rs is not None else None
    if registry is None:
        return {"pipelines": []}
    return {
        "pipelines": [
            {"name": name, "description": description or ""}
            for name, description in registry.entries()
        ]
    }


PIPELINE_LIST = ToolDefinition(
    canonical=pipeline_list_to_canonical,
    name="pipeline_list",
    # #3429: dispatched DIRECTLY by name. Before the qualified spelling was
    # abolished this tool was reached only through ``invoke_action`` (the
    # ``"__" in name`` arm of ``_invoke_router_tool``), so it never needed the
    # flag; with one name, an advertised action that lacks it lands on the
    # "unhandled tool" safety return. Pinned by
    # ``test_universal_catalog.py::test_every_catalog_action_is_directly_dispatchable``.
    router_dispatched=True,
    description=_PIPELINE_LIST_DESCRIPTION,
    parameters=_PIPELINE_LIST_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_pipeline_list,
    category="discovery",
    purity="read_only",
    # A pipeline's description is operator- OR third-party-authored text:
    # ``pipeline_install_source`` registers a pipeline straight out of
    # a fetched git repo. It is threat-scanned at install, but this tool
    # re-surfaces it on every later call, when a scan-rule update may have changed
    # the verdict. Identical rationale to ``skill_list`` (#2971), whose
    # install-from-git surface this one mirrors exactly.
    returns_external_content=True,
)

RUN_PIPELINE = ToolDefinition(
    canonical=run_pipeline_unified_to_canonical,
    name="run_pipeline",
    # #3429: dispatched DIRECTLY by name. Before the qualified spelling was
    # abolished this tool was reached only through ``invoke_action`` (the
    # ``"__" in name`` arm of ``_invoke_router_tool``), so it never needed the
    # flag; with one name, an advertised action that lacks it lands on the
    # "unhandled tool" safety return. Pinned by
    # ``test_universal_catalog.py::test_every_catalog_action_is_directly_dispatchable``.
    router_dispatched=True,
    description=_RUN_PIPELINE_DESCRIPTION,
    parameters=_RUN_PIPELINE_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_run_pipeline,
    category="io",
    purity="side_effect",
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

    registry = get_default_registry()
    for i, step in enumerate(pipeline.steps):
        # Check 2: schema REF resolves in the parsed (inline) registry.
        schema = getattr(step, "schema", None)
        if schema is not None and not schema_registry.has(schema):
            return (
                f"step {i}: schema ref {schema!r} does not resolve — no "
                "'schema:' document in the definition defines it"
            )
        # Checks 3 + 5: tool-step name resolution + S3 nested-launch deny. Mirrors
        # the run-time resolution ``_make_tool_dispatch`` performs (a registry
        # lookup of the step name) so the static verdict matches what would
        # actually dispatch.
        if isinstance(step, ToolStep):
            name = step.name
            # Check 5 (S3): reject BEFORE the registry lookup — a launch/delegate
            # verb IS a registered tool, so lookup would pass; the deny must win.
            if name in _PIPELINE_STEP_DENY_TOOLS:
                return (
                    f"step {i}: tool {name!r} is structurally denied — "
                    f"{_pipeline_step_deny_reason(name)}"
                )
            # Check 3: the tool name must resolve to a registered tool.
            if registry.lookup(name) is None:
                return (
                    f"step {i}: tool {name!r} does not resolve to a registered "
                    f"tool"
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


def _prepare_inline_pipeline(
    definition: str, *, invoker_agent: str,
) -> "tuple[dict | None, tuple[Pipeline, SchemaRegistry] | None]":
    """Parse + statically gate an ad-hoc ``definition`` DSL string (IS-4)
    into a ready-to-launch ``(Pipeline, SchemaRegistry)`` pair, or a clear
    tool error.

    Proposal 0067 P7 (#3978): narrowed from the pre-unification
    ``_prepare_inline_launch`` — the shared router-context validation
    (agent_registry/host/state_log) now lives ONCE in
    ``_handle_run_pipeline``'s prelude (both the ``name`` and ``definition``
    branches need it identically), so this helper's only job is the
    DSL-specific work: parse + static-analysis gate.

    ``schema_registry`` is NOT threaded onto ``ctx.router_state`` or any
    persistent registry — an inline definition is self-contained (its schemas
    live only in the DSL string), matching the "no persistent inline
    registry" design decision. It IS (#2572) threaded to the launch call
    (``run_pipeline_attached``/``start_pipeline_run``), which persists it onto
    the work-order (``schema_defs``) so the driver-session's ``verify:
    schema`` steps are actually enforced."""
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaError, SchemaRegistry

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
        pipeline, schema_registry, invoker_agent=invoker_agent,
    )
    if gate_error is not None:
        return {
            "status": "error",
            "data": {"error": f"inline pipeline rejected by static gate: {gate_error}"},
        }, None

    return None, (pipeline, schema_registry)


__all__ = [
    "PIPELINE_LIST",
    "RUN_PIPELINE",
]
