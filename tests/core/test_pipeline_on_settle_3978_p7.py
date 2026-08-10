"""Tier 2: proposal 0067 P7 (#3978) — ``on_settle="drop"`` reachability.

Before this PR, ``PipelineWorkOrder.on_settle`` (``work_order.py``, dataclass
default ``"deliver"``) had NO production caller that ever set it to anything
else — P7 is the first tool-call surface that threads a caller-supplied
value through (``run_pipeline(collect="async", on_settle=...)`` ->
``session_api.start_pipeline_run`` -> ``_spawn_pipeline_driver_session`` ->
``PipelineWorkOrder(on_settle=...)``). That means ``ChainManager.settle``'s
``"drop"`` branch (a bare ``pass`` — see ``chain_manager.py``) was
STRUCTURALLY UNREACHABLE from any real call path until this PR: nothing
could construct a ``WorkOrder`` with ``on_settle != "deliver"``. This file
is that reachability witness — it drives the REAL tool call
(``_handle_run_pipeline``) through to a real settle dispatch, not a
directly-constructed ``WorkOrder``/op (which cannot prove the wiring reaches
production, only that the dataclass/settle function individually work).

Real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``
throughout (no mocks) — the same harness as
``tests/core/test_pipeline_is2_driver_session.py``; the only fake is the
scripted LLM callable injected through the real ``RouterLoopDriver``
``_loop_observer`` seam. ``HookDispatcher.dispatch`` is monkeypatched to
RECORD-then-delegate (same idiom as
``tests/hooks/test_hook_event_schema_registry_sync_0059.py``) so
``task_settled`` can be observed without faking any collaborator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, TransformStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
from reyn.hooks import dispatcher as dispatcher_mod
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.tools.pipeline_verbs import _handle_run_pipeline
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn. ``calls`` is the
    reachability witness for ``on_settle="drop"``: a reply session that
    never receives a ``pipeline_result`` inbox turn never wakes its router
    loop, so ``calls`` staying 0 proves delivery was genuinely skipped (not
    just that the assertion checked the wrong inbox)."""

    def __init__(self, content: str = "unexpected — should never be called") -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _agent_registry(
    tmp_path: Path, state_log: "StateLog", scripted: "_ScriptedAgentReply | None",
) -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors the IS-2 test)."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _capture_task_settled(monkeypatch, captured: list) -> None:
    """Record every ``HookDispatcher.dispatch`` call, then delegate to the
    real implementation — same RECORD-then-delegate idiom as
    ``test_hook_event_schema_registry_sync_0059.py``."""
    original = dispatcher_mod.HookDispatcher.dispatch

    async def _recording_dispatch(self, point, template_vars):
        captured.append((point, dict(template_vars)))
        return await original(self, point, template_vars)

    monkeypatch.setattr(dispatcher_mod.HookDispatcher, "dispatch", _recording_dispatch)


async def _wait_for(predicate, *, delay: float = 0.02) -> bool:
    import asyncio
    for _ in range(500):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


@pytest.mark.asyncio
async def test_on_settle_drop_reaches_chain_manager_and_skips_delivery(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ``run_pipeline(collect="async", on_settle="drop")`` — driven
    through the REAL tool call, not a directly-constructed WorkOrder — runs
    to completion, writes the terminal marker (``delivered=False``), and
    NEVER wakes the reply session's router loop (``scripted.calls == 0``,
    the reachability witness: ``ChainManager.settle``'s ``"drop"`` branch
    was structurally unreachable before this PR, per this file's module
    docstring). RED before P7: no caller ever threaded ``on_settle`` past
    the dataclass default, so this scenario could not even be constructed
    through the tool surface."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply()
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "p", Pipeline(steps=[TransformStep(value="1 + 1", output="t0")]),
    )

    caller = reg.get_or_load("worker")
    ctx = ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry,
            agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline(
        {"name": "p", "input": None, "collect": "async", "on_settle": "drop"}, ctx,
    )
    assert result["status"] == "started", result
    run_id = result["data"]["run_id"]
    run_dir = pipeline_run_dir(tmp_path / ".reyn", run_id)

    assert await _wait_for(lambda: read_result(run_dir) is not None)
    terminal = read_result(run_dir)
    assert terminal["status"] == "ok"
    # NOTE: ``delivered`` records whether ``_deliver`` resolved a reply target
    # and executed a settle disposition through it — it stays True here
    # (the target DID resolve); it is not a claim that a ``pipeline_result``
    # message was actually posted. That's what ``scripted.calls`` below
    # proves directly, which is the real behavioral witness for "drop".
    assert terminal["delivered"] is True

    # Give the reply session's router loop a chance to wake if delivery had
    # (wrongly) happened — same settle window the async happy-path tests
    # wait on — then assert it never did.
    import asyncio
    await asyncio.sleep(0.1)
    assert scripted.calls == 0, (
        "on_settle='drop' must never post a pipeline_result to the reply "
        "session's inbox — a nonzero call count means delivery happened "
        "despite the drop disposition"
    )


@pytest.mark.asyncio
async def test_on_settle_drop_still_fires_task_settled(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ADR-0040 D4④ (architect ruling "B", 2026-08-10) — ``task_settled``
    fires on the FACT of settling, independent of whether delivery succeeded.
    This is the FIRST-EVER test with a non-default ``on_settle`` producer
    (see module docstring), so it is the first real evidence the ADR ruling
    reached the implementation rather than staying a design note nobody's
    code exercised. RED if ``_finish`` gated ``task_settled`` dispatch on
    ``_deliver``'s return value instead of firing unconditionally."""
    captured: list = []
    _capture_task_settled(monkeypatch, captured)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply()
    reg = _agent_registry(tmp_path, state_log, scripted)

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "p", Pipeline(steps=[TransformStep(value="1 + 1", output="t0")]),
    )

    caller = reg.get_or_load("worker")
    ctx = ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry,
            agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline(
        {"name": "p", "input": None, "collect": "async", "on_settle": "drop"}, ctx,
    )
    assert result["status"] == "started", result
    run_id = result["data"]["run_id"]
    run_dir = pipeline_run_dir(tmp_path / ".reyn", run_id)

    assert await _wait_for(lambda: read_result(run_dir) is not None)
    assert await _wait_for(lambda: any(p == "task_settled" for p, _ in captured))

    (point, payload), = [
        (p, v) for p, v in captured if p == "task_settled"
    ]
    assert payload["task_id"] == run_id
    assert payload["status"] == "ok"
