"""Tier 3a: #2649 — pipeline failure/cancellation envelope normalized to the
standard dispatch-error shape.

``run_pipeline``/``run_pipeline_inline``'s failed/cancelled outcome used to return
``{status: "error", data: {run_id, error: <str>}}`` (failed) / ``{status:
"cancelled", data: {run_id, error}}`` (cancelled) — the top-level ``error`` was
either absent or a plain string, never the ``{kind, message}`` dict
``router_loop.feedback()``'s dispatch-error path looks for
(``r.get("status")=="error" and isinstance(r.get("error"), dict)``), so a pipeline
failure fell through to the generic canonical-fallback rendering instead of the
``Error (<kind>): <message>`` form every other dispatch-level tool error gets.

The fix has two parts (this file drives BOTH through the real production dispatch
chokepoint, not a hand-constructed envelope):

1. ``pipeline_verbs.py``'s failed/cancelled branches now return the standard
   ``{status: "error", error: {kind, message}}`` shape (``kind`` distinguishes
   ``pipeline_failed`` from ``pipeline_cancelled`` — the two outcomes were already
   distinguished pre-fix, so the reshape does not collapse them). ``run_id`` is
   folded into ``message`` (the shape carries no third field) so it stays reachable
   for the LLM.
2. ``router_loop.feedback()``'s dispatch-error check now runs on the UNWRAPPED
   envelope (``unwrap_dispatch_envelope(r)``), not the raw ``dispatch_tool``-wrapped
   ``r`` — a tool-registry HANDLER's own returned error (a normal, non-exception
   return) is wrapped one layer deeper (``{status: "ok", data: <handler's own
   envelope>}``) by ``dispatch_tool`` before it ever reaches ``feedback()``, so the
   raw-``r`` check alone can never see it (verified empirically: pre-fix #1 alone,
   without this router_loop change, renders the ugly stringified dict
   ``"Error: {'kind': ..., 'message': ...}"``, not the terse form). Unwrapping is a
   no-op on the bare (unwrapped) dispatch-tool-own-error shapes the check already
   matched, so this is behavior-preserving for them.

No mocks: real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``/
``dispatch_tool``/``RouterLoop.feedback()`` throughout. The pipeline is driven to a
REAL failure/cancellation (an unresolvable tool step; a real step-boundary
Ctrl-C-style cancel via ``Session.cancel_inflight``), not a fabricated outcome dict.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.session_params import PresentationWiring
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools.pipeline_verbs import _handle_run_pipeline
from reyn.tools.scheme import ExecutionResult
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session


class _FeedbackHost:
    """Minimal RouterLoopHost surface ``feedback()`` reads — every extra hook
    (cap/media/scan/offload) is optional (``getattr``-guarded), so this bare host
    exercises the UN-offloaded rendering path (the assertion reads the raw string,
    not an offload-ref stub)."""

    offload_enabled = False
    agent_name = "worker"

    def __init__(self, events: EventLog) -> None:
        self.events = events


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            non_interactive=True,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


async def _dispatch_run_pipeline(args: dict, ctx: ToolContext, events: EventLog) -> dict:
    """Drive ``run_pipeline`` through the REAL production dispatch chokepoint
    (``dispatch_tool`` — the same wrap every router-issued tool call gets), not a
    hand-constructed post-dispatch envelope. Tags ``_canonical_source`` the same way
    ``RouterLoop._dispatch_resolved`` does (FP-0056 PR-F1) — this test calls
    ``dispatch_tool`` directly rather than driving a full LLM-scripted RouterLoop
    turn, so it replicates that one tagging step ``_dispatch_resolved`` would
    otherwise apply."""
    async def invoker(call_args):
        return await _handle_run_pipeline(call_args, ctx)

    dctx = DispatchContext(
        caller_kind="router", caller_id="worker", chain_id="c1",
        tool_catalog={"run_pipeline": {"function": {"name": "run_pipeline", "parameters": {}}}},
        events=events,
    )
    r = await dispatch_tool(name="run_pipeline", args=args, ctx=dctx, invoker=invoker)
    if isinstance(r, dict):
        r.setdefault("_canonical_source", "run_pipeline")
    return r


def _render(r: dict, events: EventLog) -> str:
    """The real ``RouterLoop.feedback()`` chokepoint — the SAME renderer a live chat
    turn uses to build the LLM-visible tool-result message."""
    loop = RouterLoop(host=_FeedbackHost(events), chain_id="c1", router_model="gpt-4o")
    result = ExecutionResult(
        tool_results=[r],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "run_pipeline"}}],
        assistant_content="",
    )
    msgs = loop.feedback(result)
    return next(m for m in msgs if m["role"] == "tool")["content"]


@pytest.mark.asyncio
async def test_run_pipeline_failed_renders_error_kind_message(tmp_path: Path) -> None:
    """Tier 3a: a REAL failed pipeline run (an unresolvable tool step) renders as
    ``Error (pipeline_failed): <message>`` — the standard dispatch-error form — with
    ``run_id`` still reachable in the message text (folded in, not dropped)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    agent_reg = _agent_registry(tmp_path, state_log)
    caller = agent_reg.get_or_load("worker")
    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "bad_tool_step", Pipeline(steps=[ToolStep(name="does_not_exist__nope", args={})]),
    )
    events = EventLog()
    ctx = ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path, interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry, agent_registry=agent_reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    r = await _dispatch_run_pipeline({"name": "bad_tool_step"}, ctx, events)
    content = _render(r, events)

    assert content.startswith("Error (pipeline_failed): "), content
    assert "bad_tool_step" in content
    run_id = r["data"]["error"]["message"].split("run_id: ")[1].split(")")[0]
    assert run_id.startswith("pipeline-bad_tool_step-")
    assert f"run_id: {run_id}" in content, "run_id must stay reachable for the LLM"


@pytest.mark.asyncio
async def test_run_pipeline_cancelled_renders_error_kind_message(tmp_path: Path, monkeypatch) -> None:
    """Tier 3a: a REAL step-boundary cancel (the caller's ``cancel_inflight`` —
    mirrors ``test_pipeline_is6_attached.py``'s #2588 caller→driver cancel bridge)
    renders as ``Error (pipeline_cancelled): <message>``, distinguished from
    ``pipeline_failed`` by ``kind`` (the pre-fix envelope already distinguished
    cancelled from failed via a different top-level ``status``; the reshape keeps
    the distinction, now carried by ``kind`` instead)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    step0_running = asyncio.Event()
    release_step0 = asyncio.Event()

    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        tag = str(args.get("tag", "x"))
        if tag == "s0":
            step0_running.set()
            await release_step0.wait()
        return {"tag": tag}

    tool = ToolDefinition(
        name="c2649_step",
        description="#2649 test: a gated step tool (real await gate, no side effect needed).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler, category="io", purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)

    agent_reg = _agent_registry(tmp_path, state_log)
    caller = agent_reg.get_or_load("worker")
    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "gated",
        Pipeline(steps=[
            ToolStep(name="c2649_step", args={"tag": "s0"}),
            ToolStep(name="c2649_step", args={"tag": "s1"}),
        ]),
    )
    events = EventLog()
    ctx = ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path, interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry, agent_registry=agent_reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    dispatch_task = asyncio.ensure_future(_dispatch_run_pipeline({"name": "gated"}, ctx, events))
    try:
        assert await _wait_for_event(step0_running)
        assert caller is agent_reg.get_session("worker", "main")
        await caller.cancel_inflight()
        release_step0.set()
        r = await dispatch_task
    finally:
        release_step0.set()

    content = _render(r, events)

    assert content.startswith("Error (pipeline_cancelled): "), content
    assert "gated" in content
    run_id = r["data"]["error"]["message"].split("run_id: ")[1].split(")")[0]
    assert run_id.startswith("pipeline-gated-")
    assert f"run_id: {run_id}" in content, "run_id must stay reachable for the LLM"


async def _wait_for_event(evt: asyncio.Event, timeout: float = 15.0) -> bool:
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
