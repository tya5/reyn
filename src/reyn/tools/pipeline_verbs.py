"""``run_pipeline`` router tool — IS-1 (sync + REGISTERED pipeline launch).

Per ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6: an agent
launches a REGISTERED pipeline (pre-built via
:class:`reyn.core.pipeline.registry.PipelineRegistry` — a YAML DSL parser is a
later slice) and gets its result back inline. IS-1 is deliberately narrow:

  - **Sync only.** ``run_pipeline`` runs the pipeline to completion on the
    caller's own turn via :meth:`~reyn.core.pipeline.executor.PipelineExecutor.run`
    and returns the final output — no separate driver-session spawn (R6's
    ``run_pipeline_async`` + the driver-session-per-run architecture are
    deferred; this slice proves the "launch registered pipeline, get result"
    round trip in-process).
  - **Registered only.** No ad-hoc ``run_pipeline_inline`` (deferred — needs
    the §7.3 static-analysis gate first, per R6).
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

Dependencies the handler assembles for :class:`~reyn.core.pipeline.executor.PipelineExecutor`:
  - ``registry`` (an ``AgentRegistry``, only needed if the pipeline has an
    ``AgentStep``) from ``ctx.router_state.agent_registry``.
  - ``state_log`` (for R4 step-boundary recovery) from ``ctx.state_log`` —
    the SAME process-shared WAL every other recovery-aware tool threads.
  - ``default_identity`` (the calling actor) from
    ``ctx.router_state.host.agent_name`` when a host is present.
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

import uuid
from typing import Any, Callable, Mapping

from reyn.core.pipeline.executor import PipelineExecutionError, PipelineExecutor
from reyn.core.pipeline.registry import PipelineNotFoundError
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_RUN_PIPELINE_DESCRIPTION = (
    "Run a REGISTERED pipeline by name to completion and return its final "
    "output. Blocks until the pipeline finishes (sync). 'input' seeds the "
    "pipeline's initial named context (ctx.*) for its first step. Fails "
    "clearly if 'name' is not a registered pipeline, or if any step fails."
)

_RUN_PIPELINE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The registered pipeline's name.",
        },
        "input": {
            "type": "object",
            "description": (
                "Initial named context (ctx.*) for the pipeline's first "
                "step. Omit for a pipeline that needs no seed input."
            ),
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
        return await target.handler(target_args, ctx)

    return _dispatch


async def _handle_run_pipeline(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Look up the registered pipeline, run it synchronously, return its
    final output. See the module docstring for the dependency wiring."""
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
    except PipelineNotFoundError:
        return {
            "status": "error",
            "data": {"error": f"pipeline {name!r} is not registered"},
        }

    agent_registry = rs.agent_registry if rs is not None else None
    host = rs.host if rs is not None else None
    default_identity = getattr(host, "agent_name", None) if host is not None else None
    state_log = ctx.state_log

    executor = PipelineExecutor()
    run_id = f"pipeline-{name}-{uuid.uuid4().hex}"
    try:
        result = await executor.run(
            pipeline,
            dict(raw_input) if raw_input else None,
            tool_dispatch=_make_tool_dispatch(ctx),
            state_log=state_log,
            run_id=run_id,
            registry=agent_registry,
            default_identity=default_identity,
        )
    except PipelineExecutionError as exc:
        return {
            "status": "error",
            "data": {"error": f"pipeline {name!r} failed: {exc}"},
        }

    return {
        "status": "ok",
        "data": {
            "run_id": result.run_id,
            "output": result.pipe_data,
            "named_stores": result.named_stores,
        },
    }


RUN_PIPELINE = ToolDefinition(
    name="run_pipeline",
    description=_RUN_PIPELINE_DESCRIPTION,
    parameters=_RUN_PIPELINE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline,
    category="io",
    purity="side_effect",
)


_RUN_PIPELINE_ASYNC_DESCRIPTION = (
    "Launch a REGISTERED pipeline by name in the background and return "
    "immediately with {status: started, run_id}. The pipeline runs in a "
    "dedicated crash-recoverable driver session; its final result arrives "
    "later as a [pipeline] message on your conversation. 'input' seeds the "
    "pipeline's initial named context (ctx.*) for its first step."
)


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
        )
    except ValueError as exc:
        return {"status": "error", "data": {"error": str(exc)}}

    return {"status": "started", "data": {"run_id": run_id}}


RUN_PIPELINE_ASYNC = ToolDefinition(
    name="run_pipeline_async",
    description=_RUN_PIPELINE_ASYNC_DESCRIPTION,
    parameters=_RUN_PIPELINE_PARAMETERS,  # same surface: name + optional input
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_run_pipeline_async,
    category="io",
    purity="side_effect",
)

__all__ = ["RUN_PIPELINE", "RUN_PIPELINE_ASYNC"]
