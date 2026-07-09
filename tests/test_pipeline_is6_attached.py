"""Tier 2: IS-6 sync ATTACHED pipeline driver-session — live events, no-redundant
reply, step-boundary Ctrl-C cancel, and crash-while-attached recovery.

Covers the reworked sync ``run_pipeline`` surface (a run launched into the SAME
crash-recoverable ``PipelineExecutorDriver`` driver-session as the async verb, to
which the caller ATTACHES via ``MessageBus.request``). Real collaborators
throughout — real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``;
the only fake is the scripted LLM callable injected through the real
``RouterLoopDriver`` ``_loop_observer`` seam (same discipline as
``test_pipeline_is2_driver_session.py``). No ``MagicMock``/``patch`` of
collaborators; cancel is driven through the real ``request_cancel`` /
``cancel_check`` path and a real counting tool, never a mocked step.

The four behaviors, each with a falsifiable assertion:

- **live events + inline result**: an attached run streams
  ``pipeline_step_started`` / ``pipeline_step_completed`` to the driver-session's
  own ``EventLog`` (observed via a real ``add_subscriber``), and the final output
  is returned INLINE (not via the reply inbox).
- **no redundant reply turn (§2)**: the attached happy path records
  ``delivered=False`` in the terminal marker AND leaves the caller session's inbox
  free of any ``pipeline_result`` message — the fix removes the duplicate
  unprompted turn. A positive control shows the SAME driver with
  ``notify_reply=True`` DOES deliver, so the assertion is not vacuous.
- **cancel is terminal + resumable (§3)**: a cancel observed at a step BOUNDARY
  stops before the next step (completed steps already snapshotted), the driver
  writes a TERMINAL ``cancelled`` marker (so the recovery scan does NOT
  zombie-resurrect it), the R4 journal is preserved, and an EXPLICIT
  ``executor.resume`` continues exactly-once (the pre-cancel step is not re-run).
- **crash while attached → async recovery (§4)**: a sync-launched run that
  crashes before terminal is re-woken by the recovery scan with
  ``notify_reply=True`` and delivered to the caller's inbox (sync degrades to
  async-recovery) — exactly-once.
- **TUI bridge marker + total_steps (§5, #2570)**: step events carry
  ``total_steps`` (= ``len(pipeline.steps)``), and a sync ATTACHED run, when
  given ``caller_events``, emits a ``pipeline_run_attached`` marker onto the
  CALLER's own ``EventLog`` (a different EventLog than the driver-session's,
  where the ``pipeline_step_*`` events land) right after the driver spawns —
  the bridge signal a live view (the TUI) uses to subscribe to the driver's
  events. The ASYNC path (``start_pipeline_run``) never emits this marker.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.pipeline_recovery import latest_pipeline_state
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    Pipeline,
    PipelineCancelled,
    PipelineExecutor,
    ToolStep,
)
from reyn.core.pipeline.work_order import (
    PipelineWorkOrder,
    has_result,
    pipeline_run_dir,
    read_resume_attempts,
    write_invocation,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_pipeline_attached, start_pipeline_run
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.types import ToolContext


class _ScriptedAgentReply:
    """One fixed plain-text turn — the LLM is incidental to what's under test."""

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
    *, event_sink: "list | None" = None,
) -> AgentRegistry:
    """Real AgentRegistry + real Session factory. When ``event_sink`` is given,
    every session's EventLog gets a real ``add_subscriber`` recorder (the public
    subscribe seam) so a test can observe the driver-session's live events."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None) -> Session:
        # #2708 P3.1: the attached driver spawn threads a present-sink override through the
        # factory; accept + forward it (None = Session's default self-bound consumer).
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        if event_sink is not None:
            s.router_host.events.add_subscriber(event_sink.append)
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _install_counting_tool(monkeypatch, out_file: Path, *, on_call=None) -> None:
    """Register a REAL side-effecting tool: append a line per call (so line count
    == execution count — the exactly-once probe) and, when given, invoke
    ``on_call()`` (used to fire a cancel from inside a step). Direct file write,
    workspace-independent (the driver-session's ToolContext has no workspace in
    the bare factory). Same monkeypatch idiom as the IS-2 test — every lookup
    still routes through the real ``ToolRegistry.register``/``lookup``."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args.get("tag", "x")) + "\n")
        if on_call is not None:
            on_call()
        return {"tag": str(args.get("tag", "x"))}

    tool = ToolDefinition(
        name="is6_step",
        description="IS-6 test: append a line per call (real side effect).",
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


def _bare_ctx(state_log: "StateLog | None" = None) -> ToolContext:
    from reyn.core.events.events import EventLog
    return ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None, state_log=state_log,
    )


def _steps(n: int) -> Pipeline:
    return Pipeline(steps=[
        ToolStep(name="is6_step", args={"tag": f"s{i}"}, output=f"o{i}")
        for i in range(n)
    ])


def _result_json(run_dir: Path) -> "dict | None":
    p = run_dir / "result.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


# ── attached happy path: live events + inline result ─────────────────────────


@pytest.mark.asyncio
async def test_attached_run_streams_live_events_and_returns_inline(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a sync attached run streams ``pipeline_step_started`` /
    ``pipeline_step_completed`` to the driver-session's EventLog (observed via a
    real subscriber) AND returns the final output INLINE — the emit+subscribe
    seam plus the in-band result contract. RED if the executor stopped emitting
    step events, or if the attached path stopped collecting the result."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    events: list = []
    reg = _agent_registry(tmp_path, state_log, None, event_sink=events)

    outcome = await run_pipeline_attached(
        reg,
        pipeline=_steps(2),
        pipeline_name="p",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
    )

    assert outcome["status"] == "ok"
    assert outcome["run_id"]
    # Inline result: last step's return value + the named stores, in-band.
    # #2425 PR-2: a ToolStep's ctx value is the flat text/structured shape.
    assert outcome["output"] == {"text": "", "structured": {"tag": "s1"}}
    assert outcome["named_stores"]["o0"] == {"text": "", "structured": {"tag": "s0"}}
    # Both steps really executed (exactly once each).
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0", "s1"]

    # Live events: a subscriber saw a started+completed pair for each step index,
    # tagged with the tool kind — the seam a live view / the TUI renders.
    started = [e for e in events if e.type == "pipeline_step_started"]
    completed = [e for e in events if e.type == "pipeline_step_completed"]
    assert {e.data["step_index"] for e in started} == {0, 1}
    assert {e.data["step_index"] for e in completed} == {1, 2}
    assert all(e.data["step_kind"] == "tool" for e in started + completed)


# ── §2: the attached happy path does NOT deliver a redundant reply turn ──────


@pytest.mark.asyncio
async def test_attached_run_does_not_deliver_redundant_reply_turn(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §2 regression (the crux) — after a sync attached run the driver
    records ``delivered=False`` and posts NOTHING to the caller's inbox, so the
    caller never gets an unprompted extra ``pipeline_result`` LLM turn on top of
    the inline result it already has. RED if sync were (re)wired to
    ``notify_reply=True`` — the marker would flip to ``delivered=True`` and a
    ``pipeline_result`` would land on the caller's inbox."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")  # (worker, main) = the reply address

    outcome = await run_pipeline_attached(
        reg,
        pipeline=_steps(1),
        pipeline_name="p",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
    )
    assert outcome["status"] == "ok"

    run_dir = pipeline_run_dir(tmp_path / ".reyn", outcome["run_id"])
    marker = _result_json(run_dir)
    # No inbox delivery attempted on the attached path.
    assert marker["delivered"] is False
    # ...and concretely: the caller's inbox has no pipeline_result waiting.
    drained: list = []
    while not caller.inbox.empty():
        drained.append(caller.inbox.get_nowait())
    assert not any(getattr(m, "kind", None) == "pipeline_result" for m in drained)


@pytest.mark.asyncio
async def test_notify_reply_true_driver_DOES_deliver_positive_control(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §2 positive control — the SAME driver with ``notify_reply=True``
    (the async / recovery setting) DOES post a ``pipeline_result`` to the reply
    session and records ``delivered=True`` — proving the no-duplicate assertion
    above is not vacuously true (the flag genuinely gates delivery)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    scripted = _ScriptedAgentReply("ack")
    reg = _agent_registry(tmp_path, state_log, scripted)

    sid = await reg.spawn_session_recorded("worker", mode="persistent")
    session = reg.get_session("worker", sid)
    pipeline = _steps(1)
    from reyn.core.pipeline.serde import pipeline_to_dict
    wo = PipelineWorkOrder(
        run_id="run-notify", pipeline_name="p", pipeline=pipeline_to_dict(pipeline),
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid=sid, spawn_seq=state_log.current_seq,
    )
    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-notify")
    write_invocation(run_dir, wo)
    driver = PipelineExecutorDriver(
        wo, registry=reg, state_log=state_log, notify_reply=True,
    )
    session.set_loop_driver(driver)

    await driver.run_turn("", "chain")

    marker = _result_json(run_dir)
    assert marker["status"] == "ok" and marker["delivered"] is True
    # The reply session (worker, main) actually consumed the pipeline_result.
    reg.ensure_session_running("worker", "main")
    assert await _wait_for(lambda: scripted.calls >= 1)


# ── §3: cancel is terminal + the R4 journal stays resumable ──────────────────


class _CancelAtBoundary:
    """A real ``cancel_check`` that returns True from the ``fire_at``-th poll on
    (0-indexed). No mock — a plain stateful callable modelling the driver's
    ``is_cancel_requested`` becoming True at a chosen step boundary."""

    def __init__(self, fire_at: int) -> None:
        self._fire_at = fire_at
        self.polls = 0

    def __call__(self) -> bool:
        fire = self.polls >= self._fire_at
        self.polls += 1
        return fire


@pytest.mark.asyncio
async def test_executor_cancel_at_boundary_leaves_resumable_journal(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §3 executor level — a cancel observed at the SECOND step boundary
    stops the run cleanly — step 0 ran (once), steps 1-2 did not — raising
    ``PipelineCancelled(step_index=1)``. The R4 snapshot at ``step_index=1`` is
    intact, and an EXPLICIT resume continues EXACTLY-ONCE (step 0 replayed from
    the snapshot, only steps 1-2 execute). RED if cancel fired mid-step (step 0
    would be half-applied) or if resume re-ran the completed step."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    pipeline = _steps(3)
    dispatch = _make_tool_dispatch(_bare_ctx(state_log))

    with pytest.raises(PipelineCancelled) as exc:
        await PipelineExecutor().run(
            pipeline, None, tool_dispatch=dispatch, state_log=state_log,
            run_id="run-cancel", cancel_check=_CancelAtBoundary(1),
        )
    assert exc.value.step_index == 1
    await state_log.flush()
    # Only step 0 executed before the boundary cancel.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0"]
    # The R4 snapshot is the pre-cancel resume point.
    snap = latest_pipeline_state("run-cancel", state_log)
    assert snap is not None and snap["step_index"] == 1

    # Explicit resume (no cancel): completes exactly-once — step 0 NOT re-run.
    result = await PipelineExecutor().resume(
        "run-cancel", pipeline=pipeline, tool_dispatch=dispatch, state_log=state_log,
    )
    assert result.step_index == 3
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0", "s1", "s2"]


@pytest.mark.asyncio
async def test_driver_cancel_writes_terminal_marker_recovery_skips(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §3 driver + recovery level — a Ctrl-C mid-run (a step requests
    cancel; the next boundary observes it) makes the driver write a TERMINAL
    ``cancelled`` marker with ``delivered=False`` while PRESERVING the R4
    generations. The recovery scan then does NOT re-wake it (terminal), yet an
    explicit resume can still continue exactly-once. RED if a cancelled run left
    no terminal marker (it would zombie-resurrect on the next restart) or if the
    R4 journal were discarded (no later resume possible)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    reg = _agent_registry(tmp_path, state_log, None)

    sid = await reg.spawn_session_recorded("worker", mode="persistent")
    session = reg.get_session("worker", sid)
    # Late-bind the driver so the step handler can close over it to request cancel.
    from reyn.core.pipeline.serde import pipeline_to_dict
    pipeline = _steps(3)
    wo = PipelineWorkOrder(
        run_id="run-dcancel", pipeline_name="p", pipeline=pipeline_to_dict(pipeline),
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid=sid, spawn_seq=state_log.current_seq,
    )
    driver = PipelineExecutorDriver(
        wo, registry=reg, state_log=state_log, notify_reply=False,
    )
    session.set_loop_driver(driver)
    # Step 0 requests cancel; the step-1 boundary observes it → PipelineCancelled.
    _install_counting_tool(monkeypatch, out_file, on_call=driver.request_cancel)
    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-dcancel")
    write_invocation(run_dir, wo)

    await driver.run_turn("", "chain")

    marker = _result_json(run_dir)
    assert marker is not None and marker["status"] == "cancelled"
    assert marker["delivered"] is False
    assert has_result(run_dir)  # TERMINAL — recovery must treat it as done.
    # Only step 0 ran; the R4 snapshot preserves the resume point.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0"]
    assert latest_pipeline_state("run-dcancel", state_log)["step_index"] == 1

    # Recovery scan does NOT resurrect a terminally-cancelled run.
    rewoken = await reg._rewake_pipeline_runs()
    assert "run-dcancel" not in rewoken
    assert read_resume_attempts(run_dir) == 0

    # The preserved journal still supports an EXPLICIT resume, exactly-once.
    result = await PipelineExecutor().resume(
        "run-dcancel", pipeline=pipeline,
        tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)), state_log=state_log,
    )
    assert result.step_index == 3
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0", "s1", "s2"]


# ── §3b: the ATTACHED CALLER's Ctrl-C reaches the driver (#2588) ─────────────


@pytest.mark.asyncio
async def test_attached_caller_cancel_inflight_stops_pipeline_at_boundary(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: #2588 — a Ctrl-C on the ATTACHED CALLER session drives the cancel
    through the CALLER's public ``cancel_inflight`` seam (NOT a direct
    ``driver.request_cancel``), and it must reach the spawned driver-session so
    the pipeline STOPS at the next step boundary: step 1 (index 1) never runs and
    the caller collects ``status="cancelled"`` inline. This is the end-to-end
    caller→driver bridge IS-6 lacked. RED against pre-#2588 main — there the
    caller's ``cancel_inflight`` only cancelled the caller's OWN turn-driver, the
    driver-session never saw it, both steps ran, and the result was ``ok``.

    A real gate (``asyncio.Event``) makes step 0 an AWAITING step so a concurrent
    task can fire the caller cancel WHILE step 0 is in flight and the event loop
    is free — the same shape a human Ctrl-C takes against a running pipeline. No
    mock: real Session/AgentRegistry/StateLog/MessageBus/PipelineExecutor and a
    real gated tool; the cancel is driven only through ``Session.cancel_inflight``.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"

    step0_running = asyncio.Event()
    release_step0 = asyncio.Event()

    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        tag = str(args.get("tag", "x"))
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(tag + "\n")
        if tag == "s0":  # gate step 0 so the loop is free for a concurrent cancel
            step0_running.set()
            await release_step0.wait()
        return {"tag": tag}

    tool = ToolDefinition(
        name="is6_step",
        description="#2588 test: a gated step tool (real side effect + await gate).",
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

    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")  # (worker, main) — the reply/caller address

    run_task = asyncio.ensure_future(run_pipeline_attached(
        reg,
        pipeline=_steps(2),
        pipeline_name="p",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
    ))
    try:
        # Wait until step 0 is really in flight (started + awaiting the gate).
        assert await _wait_for(step0_running.is_set)
        # THE BUG UNDER TEST: cancel via the CALLER session's public seam — the
        # SAME instance run_pipeline_attached resolves as the reply address. Not
        # a direct driver.request_cancel.
        assert caller is reg.get_session("worker", "main")  # same live instance
        await caller.cancel_inflight()
        # Release step 0 → the executor reaches the step-1 boundary and observes
        # the now-forwarded cancel.
        release_step0.set()
        outcome = await run_task
    finally:
        release_step0.set()  # never wedge the loop if an assertion above fired

    assert outcome["status"] == "cancelled"
    # Step 1 (index 1) never executed — only step 0's line is present.
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0"]
    # Terminal cancelled marker on disk (so recovery won't resurrect it).
    run_dir = pipeline_run_dir(tmp_path / ".reyn", outcome["run_id"])
    marker = _result_json(run_dir)
    assert marker is not None and marker["status"] == "cancelled"


# ── §4: crash while attached → recovery re-wakes + delivers via inbox ────────


@pytest.mark.asyncio
async def test_crash_while_attached_recovers_and_delivers_via_inbox(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §4 — a SYNC-launched run (work-order reply = the attached caller,
    driver ``notify_reply=False``) that crashes before terminal is re-woken by
    the recovery scan with ``notify_reply=True`` and delivered to the caller's
    inbox — sync degrades to async-recovery, exactly-once. RED if recovery
    inherited the sync ``notify_reply=False`` (the result would be lost — no
    inline caller to collect it after a crash), or if it re-ran a completed
    step."""
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    full = _steps(3)

    # Crash state: steps 0-1 completed (R4 gens on disk), NO terminal marker —
    # the process died mid-attach. The work-order is the SYNC-path shape (its
    # driver was notify_reply=False; that flag is NOT persisted).
    prefix = Pipeline(steps=list(full.steps[:2]))
    await PipelineExecutor().run(
        prefix, None, tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)),
        state_log=state_log, run_id="run-crash",
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0", "s1"]

    from reyn.core.pipeline.serde import pipeline_to_dict
    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-crash")
    write_invocation(run_dir, PipelineWorkOrder(
        run_id="run-crash", pipeline_name="p", pipeline=pipeline_to_dict(full),
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv-crash", spawn_seq=None,
    ))

    # Restart: fresh StateLog + registry with a scripted caller, then recover.
    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("recovered")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    marker = _result_json(run_dir)
    assert marker["status"] == "ok"
    # Recovery delivered via inbox (notify_reply flipped to True on re-wake).
    assert marker["delivered"] is True
    # Exactly-once: steps 0-1 replayed from the snapshot, only step 2 executed.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["s0", "s1", "s2"]
    # The caller consumed the re-delivered pipeline_result.
    assert await _wait_for(lambda: scripted.calls >= 1)


# ── §5: TUI bridge marker + total_steps (#2570) ───────────────────────────────


@pytest.mark.asyncio
async def test_attached_run_emits_bridge_marker_on_callers_own_eventlog(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §5 — a sync attached run, given ``caller_events``, emits a
    ``pipeline_run_attached`` marker onto the CALLER's own EventLog (observed
    via a real subscriber on the caller session, NOT the driver-session) right
    after the driver-session spawns, carrying the run_id/driver_sid/agent_name/
    pipeline_name/tool the TUI needs to bridge-subscribe to the driver's own
    events. RED if the marker were dropped, emitted on the wrong EventLog, or
    missing a field the bridge contract requires."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")  # (worker, main) = the reply/caller address
    caller_events: list = []
    caller.router_host.events.add_subscriber(caller_events.append)

    outcome = await run_pipeline_attached(
        reg,
        pipeline=_steps(2),
        pipeline_name="p",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        tool="run_pipeline",
        caller_events=caller.router_host.events,
    )
    assert outcome["status"] == "ok"

    markers = [e for e in caller_events if e.type == "pipeline_run_attached"]
    (marker,) = markers  # exactly one — unpack fails RED if 0 or >1 landed
    assert marker.data["tool"] == "run_pipeline"
    assert marker.data["run_id"] == outcome["run_id"]
    assert marker.data["agent_name"] == "worker"
    assert marker.data["pipeline_name"] == "p"
    driver_sid = marker.data["driver_sid"]
    assert isinstance(driver_sid, str) and driver_sid

    # The driver_sid actually names a real, distinct session (the driver, not
    # the caller's own "main" sid) whose EventLog carries the step events.
    assert driver_sid != "main"
    driver_session = reg.get_session("worker", driver_sid)
    assert driver_session is not None
    driver_events = driver_session.router_host.events.all()
    started = [e for e in driver_events if e.type == "pipeline_step_started"]
    completed = [e for e in driver_events if e.type == "pipeline_step_completed"]
    assert {e.data["step_index"] for e in started} == {0, 1}
    assert {e.data["step_index"] for e in completed} == {1, 2}
    assert all(e.data["total_steps"] == 2 for e in started + completed)
    assert all(e.data["step_kind"] == "tool" for e in started + completed)


@pytest.mark.asyncio
async def test_async_launch_does_not_emit_bridge_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: §5 negative — the ASYNC launch path (``start_pipeline_run``) has
    no attached live viewer to bridge, so it must never emit the
    ``pipeline_run_attached`` marker onto the caller's EventLog. RED if the
    marker leaked onto an async-launched caller's events (it has no
    ``caller_events``/``tool`` param at all — this asserts the runtime effect,
    not just the absent parameter)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_counting_tool(monkeypatch, out_file)
    scripted = _ScriptedAgentReply("ack")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    caller_events: list = []
    caller.router_host.events.add_subscriber(caller_events.append)

    rid = await start_pipeline_run(
        reg,
        pipeline=_steps(1),
        pipeline_name="p",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
    )
    assert rid

    # Let the detached pump run to completion so the async result actually
    # reaches the caller's inbox — the marker-absence check covers the whole
    # async lifecycle, not just the launch instant.
    assert await _wait_for(lambda: scripted.calls >= 1)
    assert not any(e.type == "pipeline_run_attached" for e in caller_events)
