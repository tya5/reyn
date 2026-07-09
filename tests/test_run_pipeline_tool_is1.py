"""Tier 2c: run_pipeline router tool (sync ATTACHED driver-session launch).

Covers ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6's sync
launch verb: an agent (here, the tool handler's caller) launches a REGISTERED
pipeline and gets its result inline. IS-6 reworked the sync surface from an
inline ``PipelineExecutor().run`` to an ATTACHED driver-session (so a sync run
is crash-recoverable) — this file exercises the reworked handler's contract:
the full-wiring requirement (agent_registry + host + WAL, like the async verb),
the inline ``{run_id, output, named_stores}`` result, and the early-validation
error paths. The step-boundary live-events / Ctrl-C-cancel / crash-while-attached
behaviors get their own file (``test_pipeline_is6_attached.py``).

Real collaborators throughout — a real ``PipelineRegistry``, real ``Workspace``/
``PermissionResolver`` (so the ``tool`` step's ``file__write`` actually writes
through ``op_runtime.execute_op``, not a stub), and a real ``AgentRegistry``/
``Session`` for the driver-session + ``agent`` step (same real-collaborator
discipline as ``test_pipeline_is2_driver_session.py`` — the ONLY faked
collaborator is the LLM completion call, injected via the real
``RouterLoopDriver`` ``_loop_observer`` seam).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import AgentStep, ExprRef, Pipeline, ToolStep, TransformStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.data.workspace.workspace import Workspace
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import _build_agent_step_narrowing
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools.pipeline_verbs import _handle_run_pipeline
from reyn.tools.types import RouterCallerState, ToolContext


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn (no tool_calls) — the
    LLM is incidental to what's under test (the run_pipeline composition),
    same rationale as test_pipeline_r5_agent_step_executor.py."""

    def __init__(self, content: str) -> None:
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
    """Real AgentRegistry + real Session factory (mirrors
    test_pipeline_r5_agent_step_executor.py's ``_registry`` helper)."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        # #2708 P3.1: accept + forward the attached driver spawn's present-sink override.
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,  # #2708 P3.2a: accept + forward the attached driver spawn's intervention bridge
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


def _ctx(
    tmp_path: Path,
    *,
    pipeline_registry: "PipelineRegistry | None" = None,
    agent_registry: "AgentRegistry | None" = None,
    state_log: "StateLog | None" = None,
    host: "object | None" = None,
) -> ToolContext:
    events = host.events if host is not None else EventLog()
    return ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path,
            interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry,
            agent_registry=agent_registry,
            host=host,
        ),
        state_log=state_log,
    )


# ── end-to-end: transform -> tool -> agent ──────────────────────────────────


def _install_side_effect_tool(monkeypatch, out_file: Path) -> None:
    """Register a REAL side-effecting tool (direct file append, workspace-
    independent — the driver-session's ToolContext has no workspace in this bare
    factory, so a workspace-relative tool like file__write would not resolve to
    this test's tmp dir). Same monkeypatch idiom as
    ``test_pipeline_is2_driver_session.py``: every lookup still goes through the
    real ``ToolRegistry.register``/``lookup`` contract."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args["content"]) + "\n")
        return {"written": str(args["content"])}

    tool = ToolDefinition(
        name="is1_write",
        description="IS-1 test: append content to a fixed file (real side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler,
        category="io",
        purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


@pytest.mark.asyncio
async def test_run_pipeline_e2e_transform_tool_agent(tmp_path: Path, monkeypatch) -> None:
    """Tier 2c: register a transform->tool->agent pipeline, invoke the reworked
    run_pipeline handler (attached driver-session) with a real host/AgentRegistry/
    StateLog, assert the pipeline runs to completion, the tool step's file__write
    REALLY wrote the file (op_runtime execute_op, not a stub), and the final
    output is the agent step's (scripted) reply — returned INLINE.

    The scripted LLM is called EXACTLY ONCE (the agent step). The driver-session
    and the caller session invoke no LLM, so ``scripted.calls == 1`` is also an
    implicit check that the attached path did not trigger a redundant reply turn
    on the caller (the dedicated no-duplicate assertion lives in the IS-6 file)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("the pipeline's final answer")
    agent_reg = _agent_registry(tmp_path, state_log, scripted)
    caller = agent_reg.get_or_load("worker")

    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "greet_and_review",
        Pipeline(steps=[
            TransformStep(value="'hello ' + ctx.name", output="msg"),
            ToolStep(
                name="is1_write",
                args={"content": ExprRef("pipe")},
                output="write_result",
            ),
            AgentStep(prompt="review: {ctx.msg}", identity="worker", output="verdict"),
        ]),
    )

    ctx = _ctx(
        tmp_path, pipeline_registry=pipeline_registry,
        agent_registry=agent_reg, state_log=state_log, host=caller._router_host,
    )

    result = await _handle_run_pipeline(
        {"name": "greet_and_review", "input": {"name": "world"}}, ctx,
    )

    assert result["status"] == "ok"
    assert result["data"]["output"] == "the pipeline's final answer"
    assert result["data"]["named_stores"]["msg"] == "hello world"
    assert scripted.calls == 1
    # the tool step ran a REAL side effect (not a stubbed pass-through).
    assert out_file.read_text().splitlines() == ["hello world"]


# ── missing pipeline ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_pipeline_missing_name_returns_clear_error(tmp_path: Path) -> None:
    """Tier 2: run_pipeline("nonexistent") -> a clear tool error, not a raw
    KeyError / unhandled exception."""
    ctx = _ctx(tmp_path, pipeline_registry=PipelineRegistry())

    result = await _handle_run_pipeline({"name": "nonexistent"}, ctx)

    assert result["status"] == "error"
    assert "nonexistent" in result["data"]["error"]
    assert "not registered" in result["data"]["error"]


@pytest.mark.asyncio
async def test_run_pipeline_empty_name_returns_clear_error(tmp_path: Path) -> None:
    """Tier 2: an empty/missing 'name' arg fails clearly, not a KeyError."""
    ctx = _ctx(tmp_path, pipeline_registry=PipelineRegistry())

    result = await _handle_run_pipeline({}, ctx)

    assert result["status"] == "error"
    assert "name" in result["data"]["error"]


@pytest.mark.asyncio
async def test_run_pipeline_no_registry_returns_clear_error(tmp_path: Path) -> None:
    """Tier 2: no PipelineRegistry threaded via ctx.router_state -> a clear
    error (not an AttributeError on a None registry)."""
    ctx = _ctx(tmp_path)  # pipeline_registry defaults to None

    result = await _handle_run_pipeline({"name": "anything"}, ctx)

    assert result["status"] == "error"
    assert "PipelineRegistry" in result["data"]["error"]


# ── tool-step failure surfaces as a clear pipeline error ────────────────────


@pytest.mark.asyncio
async def test_run_pipeline_unknown_tool_step_fails_clearly(tmp_path: Path) -> None:
    """Tier 2: a ToolStep naming an unresolvable tool fails the run with a
    clear error (the real tool_dispatch seam in the driver-session, not a silent
    no-op) — the failure surfaces INLINE to the attached caller."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    agent_reg = _agent_registry(tmp_path, state_log, None)
    caller = agent_reg.get_or_load("worker")
    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "bad_tool_step",
        Pipeline(steps=[ToolStep(name="does_not_exist__nope", args={})]),
    )
    ctx = _ctx(
        tmp_path, pipeline_registry=pipeline_registry,
        agent_registry=agent_reg, state_log=state_log, host=caller._router_host,
    )

    result = await _handle_run_pipeline({"name": "bad_tool_step"}, ctx)

    assert result["status"] == "error"
    assert "bad_tool_step" in result["data"]["error"]


# ── S3: agent-step narrowing structurally denies run_pipeline ───────────────


def test_agent_step_narrowing_denies_run_pipeline() -> None:
    """Tier 2: OS invariant (R6 S3) — a pipeline's agent step is a spawn-tree
    LEAF: its narrowing structurally denies run_pipeline (nesting is
    call-only), same posture as the pre-existing delegate_to_agent deny.
    Purely structural — inspects the deny-set the narrowing builds."""
    narrowing = _build_agent_step_narrowing(None)
    assert "run_pipeline" in narrowing["tool_deny"]
    assert "delegate_to_agent" in narrowing["tool_deny"]
