"""Tier 2: #2570 — TUI live-progress bridge for a sync-attached run_pipeline.

IS-6 (#2569) made a sync ``run_pipeline`` call block the caller's turn while a
crash-recoverable driver-session runs the pipeline, emitting
``pipeline_step_started`` / ``pipeline_step_completed`` (with ``total_steps``)
onto the DRIVER-session's own EventLog. That EventLog belongs to a session
distinct from the human-attached caller, so the TUI (subscribed only to the
caller's own ``_chat_events``) had no signal to bridge-subscribe to.

``session_api.run_pipeline_attached`` closes that gap: when given
``caller_events``, it emits a ``pipeline_run_attached`` marker
({tool, run_id, driver_sid, agent_name, pipeline_name}) onto the CALLER's own
EventLog right after the driver-session spawns. ``ChatLifecycleForwarder``
(this test's subject) is the subscriber that turns that marker into a live
bridge-subscription: it looks up the driver session via the injected registry,
subscribes to its EventLog for the run's duration, and forwards each step
boundary as a transient ``status`` outbox line (mirroring ``on_mcp_progress`` —
a many-step pipeline would spam permanent ``system`` markers otherwise).
Unsubscribes when the matching ``run_pipeline`` tool call completes (fed via
``on_tool_returned`` / ``on_tool_failed``, which the real dispatcher would call
in production) — proving no subscriber leak across successive attached runs.

Real ``AgentRegistry`` / ``Session`` / ``StateLog`` / ``Pipeline`` /
``TransformStep`` throughout — no mocks. Transform-only pipelines are pure
(no LLM/tool dependency), keeping the test focused on the bridge mechanics.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, TransformStep
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_pipeline_attached
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors the IS-2/IS-4 tests)."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        # #2708 P3.1: accept + forward the attached driver spawn's present-sink override.
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,  # #2708 P3.2a: accept + forward the attached driver spawn's intervention bridge
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _two_step_transform_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            TransformStep(value="ctx.seed + 1", output="t0"),
            TransformStep(value="ctx.t0 + 1", output="t1"),
        ],
        description="#2570 test pipeline",
    )


async def _run_attached(
    reg: AgentRegistry, state_log: "StateLog", caller_events: "EventLog",
) -> dict:
    return await run_pipeline_attached(
        reg,
        pipeline=_two_step_transform_pipeline(),
        pipeline_name="testpipe",
        input={"seed": 0},
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        tool="run_pipeline",
        caller_events=caller_events,
    )


@pytest.mark.asyncio
async def test_attached_run_streams_step_progress_to_caller_outbox(tmp_path) -> None:
    """Tier 2: a sync-attached run streams transient status lines for each step
    boundary to the CALLER's outbox — proving the bridge-subscribe actually
    reaches the driver-session's own EventLog (a different session)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    outbox: asyncio.Queue = asyncio.Queue()
    caller_events = EventLog()
    caller_events.add_subscriber(ChatLifecycleForwarder(outbox, registry=reg))

    outcome = await _run_attached(reg, state_log, caller_events)

    assert outcome["status"] == "ok"
    msgs = _drain(outbox)
    status_msgs = [m for m in msgs if m.kind == "status" and m.meta.get("source") == "pipeline"]
    # Unpack-enforcement: a 2-step pipeline produces exactly one status line
    # per step boundary (started + completed) — destructuring fails loudly if
    # a step boundary is missed or double-fired.
    step1_started, step1_done, step2_started, step2_done = [m.text for m in status_msgs]
    assert step1_started == "[▸ testpipe: step 1/2 (transform)]"
    assert step1_done == "[✓ testpipe: step 1/2 (transform) done]"
    assert step2_started == "[▸ testpipe: step 2/2 (transform)]"
    assert step2_done == "[✓ testpipe: step 2/2 (transform) done]"


@pytest.mark.asyncio
async def test_unsubscribe_stops_further_progress_after_tool_completion(tmp_path) -> None:
    """Tier 2: once the matching run_pipeline tool call completes (simulated —
    the real dispatcher emits tool_returned after the handler returns), a
    LATER synthetic step event on the driver's own EventLog produces NO further
    outbox message — proving the bridge unsubscribes (no listener leak across
    successive attached runs)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    outbox: asyncio.Queue = asyncio.Queue()
    caller_events = EventLog()
    fwd = ChatLifecycleForwarder(outbox, registry=reg)
    caller_events.add_subscriber(fwd)
    recorded: list[Event] = []
    caller_events.add_subscriber(recorded.append)

    outcome = await _run_attached(reg, state_log, caller_events)
    _drain(outbox)  # the real run's own progress lines — not under test here

    attach_marker = next(e for e in recorded if e.type == "pipeline_run_attached")
    driver_sid = attach_marker.data["driver_sid"]
    run_id = attach_marker.data["run_id"]
    assert run_id == outcome["run_id"]

    driver_session = reg.get_session("worker", driver_sid)
    driver_events = driver_session.router_host.events

    # Still bridged immediately after the run (unsubscribe only fires on
    # tool_returned/tool_failed, which we haven't simulated yet).
    driver_events.emit(
        "pipeline_step_started", run_id=run_id, step_index=5, step_kind="transform",
        total_steps=6,
    )
    assert len(_drain(outbox)) == 1

    # Simulate the dispatcher's post-invocation event (on_tool_returned) — this
    # itself enqueues its own tool_call_completed line, drained here so it
    # doesn't confound the assertion below.
    fwd(Event(type="tool_returned", data={"tool": "run_pipeline", "result": outcome}))
    _drain(outbox)

    # Now unsubscribed: a further step event on the SAME driver EventLog must
    # produce no pipeline-status line.
    driver_events.emit(
        "pipeline_step_completed", run_id=run_id, step_index=6, step_kind="transform",
        total_steps=6,
    )
    status_msgs = [m for m in _drain(outbox) if m.kind == "status"]
    assert status_msgs == []


@pytest.mark.asyncio
async def test_no_registry_is_a_graceful_noop(tmp_path) -> None:
    """Tier 2: ChatLifecycleForwarder(outbox) with no registry (the pre-#2570
    default) drops pipeline_run_attached silently — forward-compat / graceful
    degrade, no exception."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    outbox: asyncio.Queue = asyncio.Queue()
    caller_events = EventLog()
    caller_events.add_subscriber(ChatLifecycleForwarder(outbox))  # no registry=

    outcome = await _run_attached(reg, state_log, caller_events)

    assert outcome["status"] == "ok"
    status_msgs = [m for m in _drain(outbox) if m.kind == "status"]
    assert status_msgs == []
