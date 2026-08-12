"""Tier 2: #4215 ② — an ATTACHED pipeline driver's hook-bus events reach the
PARENT session's own bus, non-blocking, without going through either
session's HookDispatcher.

Owner ruling on #4215 (corrected framing, relayed by lead-coder): the
concern was never "a parent must not observe a child" — it was "there must
be a CHOICE, not a structural impossibility". `bridge_child_bus_to_parent`
(`reyn.hooks.bus`) is that choice: wired ONLY at the ATTACHED spawn
(`session_api._spawn_pipeline_driver_session`, mirroring the existing
`SpawnBridgePresentationConsumer`/`SpawnBridgeInterventionListener`
pattern), a background task that subscribes to the child's bus and calls
`parent_bus.publish` directly for every event — never through
`HookDispatcher` (whose own `dispatch_bus_event` already documents why
re-publishing a bus-originated event onto a bus would double-deliver it).

Real `AgentRegistry`/`Session`/`StateLog` throughout — no collaborator
mocks, matching this arc's own established discipline (test_2708_p32a's own
module docstring).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.hooks.bus import HookBus, bridge_child_bus_to_parent
from reyn.hooks.event import HookEvent
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import _spawn_pipeline_driver_session
from reyn.runtime.session_params import PresentationWiring
from tests._async_wait import wait_until
from tests._support.agent_session import make_session


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Mirrors test_2708_p32a_spawn_bridge_intervention.py's own factory —
    the widened factory protocol accepting BOTH spawn overrides."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge,
            ),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _noop_pipeline() -> Pipeline:
    # A single step is enough — this test never runs the pipeline to
    # completion, only spawns its driver-session.
    return Pipeline(steps=[ToolStep(name="noop", args={}, output="out")])


@pytest.mark.asyncio
async def test_attached_spawn_bridges_child_hook_events_to_the_parent(tmp_path: Path) -> None:
    """Tier 2: an event published on the CHILD driver's hook-bus is observed
    on the PARENT's bus — the real, end-to-end consequence of the bridge
    task the ATTACHED spawn now starts."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    parent = reg.get_or_load("worker")

    driver, _rid, _sid = await _spawn_pipeline_driver_session(
        reg,
        pipeline=_noop_pipeline(),
        pipeline_name="noop",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        notify_reply=False,
        attached_parent_session=parent,
    )
    # The bridge task's own `child_bus.subscribe()` must actually run
    # before a publish on the child can reach it — HookBus is a broadcast
    # bus with no replay for a subscriber that registers late (bus.py's
    # own module docstring). `asyncio.create_task` schedules but does not
    # immediately run the new task, so wait for its subscription to
    # actually register (the public `subscriber_count` surface, reached
    # via `driver._hook_bus` — the established convention roughly 20
    # other tests in this repo already use, since there is no public
    # `hook_bus` property). This wait doubles as the "a bridge task
    # started" proof itself — no dedicated public surface needed for
    # that fact alone.
    try:
        await wait_until(lambda: driver._hook_bus.subscriber_count >= 1)

        async with parent._hook_bus.subscribe() as parent_sub:
            probe = HookEvent(
                kind="builtin:external:test_probe",
                payload={"marker": "reyn-4215-probe"},
            )
            driver._hook_bus.publish(probe)
            received = await asyncio.wait_for(parent_sub.get(), timeout=5.0)

        assert received.payload.get("marker") == "reyn-4215-probe"
    finally:
        driver._hook_bus_bridge_task.cancel()


@pytest.mark.asyncio
async def test_detached_spawn_starts_no_bridge_task(tmp_path: Path) -> None:
    """Tier 2: scope guard — a DETACHED spawn (no live parent to bridge to,
    `attached_parent_session=None`) starts no bridge task at all. Without
    this, a detached driver would silently subscribe to nothing useful (its
    own events forwarded nowhere) while still paying a live task's cost.

    Observed via ``HookBus.subscriber_count`` (already public) rather than
    a dedicated Session-level property — a public surface is a means to
    avoid a private-state assertion, not a goal in itself, and a session's
    own hook-bus subscriber count already says everything this test needs
    without growing Session's own public member ceiling for a single
    test's sake."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)

    driver, _rid, _sid = await _spawn_pipeline_driver_session(
        reg,
        pipeline=_noop_pipeline(),
        pipeline_name="noop",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        notify_reply=False,
        attached_parent_session=None,
    )
    # Give a wrongly-started bridge task a real chance to subscribe before
    # asserting its absence (same "don't race a real async effect" shape
    # as the other tests in this file).
    await asyncio.sleep(0.1)
    subscriber_count = driver._hook_bus.subscriber_count
    assert subscriber_count == 0, (
        "a DETACHED spawn must not start a hook-bus bridge task — nothing "
        "should have subscribed to the driver's own bus"
    )


@pytest.mark.asyncio
async def test_removing_the_child_session_cancels_its_bridge_task(tmp_path: Path) -> None:
    """Tier 2: teardown — AgentRegistry.remove_session cancels a bridged
    child's bridge task. Without this, every attached pipeline run would
    leak one live background task per invocation for the life of the
    process (the exact class of bug the #4376 image-cache fix, landed
    minutes before this, exists to prevent for a different resource).

    Observed via ``HookBus.subscriber_count`` (already public), not the
    task object itself: cancelling ``bridge_child_bus_to_parent`` unwinds
    its ``async with child_bus.subscribe()`` block, which calls
    ``close()`` — the count going back to 0 IS the externally-visible
    consequence of the cancel actually running (same reasoning as the
    scope-guard test above)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    parent = reg.get_or_load("worker")

    driver, _rid, sid = await _spawn_pipeline_driver_session(
        reg,
        pipeline=_noop_pipeline(),
        pipeline_name="noop",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        notify_reply=False,
        attached_parent_session=parent,
    )
    await wait_until(lambda: driver._hook_bus.subscriber_count >= 1)

    await reg.remove_session("worker", sid)

    await wait_until(lambda: driver._hook_bus.subscriber_count == 0)


def test_bridge_child_bus_to_parent_never_touches_a_hook_dispatcher() -> None:
    """Tier 2: bridge_child_bus_to_parent's own contract — it takes exactly
    two HookBus instances, nothing dispatcher-shaped. A future edit routing
    it through a HookDispatcher (the double-delivery bug
    dispatch_bus_event's own docstring warns against) would change this
    signature, which this test pins directly rather than trusting the
    docstring alone."""
    import inspect

    sig = inspect.signature(bridge_child_bus_to_parent)
    # `from __future__ import annotations` (bus.py) makes every annotation a
    # plain string at runtime, not the class object, and the source itself
    # quotes each one ("HookBus") — strip both layers of quoting rather
    # than pin the exact quote style, which isn't this test's subject.
    param_types = [str(p.annotation).strip("'\"") for p in sig.parameters.values()]
    assert param_types == ["HookBus", "HookBus"], (
        f"bridge_child_bus_to_parent's signature changed shape: {param_types!r} "
        "— if this now accepts a dispatcher, re-read dispatch_bus_event's own "
        "docstring on why that would double-deliver."
    )


@pytest.mark.asyncio
async def test_bridge_publish_never_blocks_the_child(tmp_path: Path) -> None:
    """Tier 2: HookBus.publish is documented as synchronous and
    non-blocking (bus.py's own module docstring) — this exercises that
    property specifically THROUGH the bridge: publishing many events in a
    tight loop on the child returns immediately regardless of whether the
    bridge task has had a chance to drain any of them yet."""
    child = HookBus()
    parent = HookBus()
    bridge = asyncio.ensure_future(bridge_child_bus_to_parent(child, parent))
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i in range(50):
            child.publish(
                HookEvent(kind="builtin:external:test_probe", payload={"i": i})
            )
        elapsed = loop.time() - start
        assert elapsed < 0.5, (
            f"50 publishes on the bridged child took {elapsed:.3f}s — "
            "publish must never block on the bridge task draining"
        )
    finally:
        bridge.cancel()
