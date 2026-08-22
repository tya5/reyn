"""Tier 2: A2A + MCP progress bridges don't retain finished notification
tasks for the life of the call (#3390).

Both ``_A2AProgressBridge`` and ``_MCPProgressBridge`` schedule one
``asyncio.Task`` per forwarded audit-event (``on_event`` / ``_on_event``
calls ``asyncio.ensure_future`` and appends the result to ``self._tasks``).
Before this fix, nothing ever removed a finished entry — ``detach()`` only
cancelled tasks that were ``not task.done()`` — so the list grew
monotonically with every tracked audit-event the bridge ever saw, for the
life of the bridge (= one ``send_to_agent`` call / one async A2A run, per
#3390's stated scope — not the whole process).

The size was never asserted anywhere, which is how the leak survived: the
per-call count used to track 1:1 with LLM calls; after #3389 it also tracks
tool-dispatch chokepoint audit-events, a much higher-volume source. This
module drives N tracked audit-events through a real ``EventLog`` and
asserts the bridge's ``tracked_task_count`` — the public read surface added
alongside the fix, following the existing ``detached`` precedent rather
than reaching into ``_tasks`` directly — stays bounded instead of growing
with N.

Strip-falsify: removing the ``task.add_done_callback(self._tasks.discard)``
call at either bridge's append site (while keeping the ``set`` -> the
production call-site the fix touches) makes the corresponding test in this
module RED, growing ``tracked_task_count`` linearly with N.
"""
from __future__ import annotations

import asyncio

from reyn.core.events.events import EventLog

_N = 25


async def _drain(iterations: int = 3) -> None:
    """Yield to the loop enough times for a same-tick task to finish and
    its done callback (= call_soon, never synchronous) to fire."""
    for _ in range(iterations):
        await asyncio.sleep(0)


# ── 1. A2A bridge ───────────────────────────────────────────────────────


def test_a2a_bridge_tracked_task_count_does_not_grow_with_event_count() -> None:
    """Tier 2: driving N ``tool_returned`` audit-events through a real
    ``EventLog`` leaves the A2A bridge's ``tracked_task_count`` bounded,
    not proportional to N.

    ``webhook_url=None`` so ``_send`` only does the always-on SSE-buffer
    append (real ``RunRegistry``) — no network I/O, so every scheduled
    task finishes on its first step and the done callback is what removes
    it.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge
    from reyn.interfaces.web.run_registry import RunRegistry

    events = EventLog()

    class _SessionWithChatEvents:
        _audit_events = events

    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="demo", chain_id="c1")
    bridge = _A2AProgressBridge(
        session=_SessionWithChatEvents(),
        run_id=entry.run_id,
        webhook_url=None,
        agent_name="demo",
        run_registry=run_registry,
    )

    async def _drive() -> int:
        bridge.attach()
        try:
            for _ in range(_N):
                events.emit("tool_returned", tool="grep", chain_id="c1")
                await _drain()
            return bridge.tracked_task_count
        finally:
            bridge.detach()

    final_count = asyncio.run(_drive())

    assert final_count < _N, (
        f"tracked_task_count ({final_count}) grew with the {_N} driven "
        "events instead of staying bounded — finished tasks are not "
        "being removed from _tasks"
    )
    # Every event's task had a chance to finish + discard itself; nothing
    # concurrent is still in flight.
    assert final_count == 0
    forwarded = run_registry.get(entry.run_id).history_events
    assert len(forwarded) == _N


# ── 2. MCP bridge ───────────────────────────────────────────────────────


def test_mcp_bridge_tracked_task_count_does_not_grow_with_event_count() -> None:
    """Tier 2: driving N ``tool_returned`` audit-events through a real
    ``EventLog`` leaves the MCP bridge's ``tracked_task_count`` bounded,
    not proportional to N. Mirrors the A2A case above (#3390 is one
    defect, two files)."""
    # #5058: mcp is a core dependency (mcp>=2.0,<3.0, #4412) -- an
    # importorskip here was a silent skip on a broken install, not a
    # normal absent-extra path (architect ruling, gh issue view 5058:
    # "the correct behavior is red"). Removed.
    from reyn.mcp.server import _MCPProgressBridge

    events = EventLog()

    class _SessionWithChatEvents:
        _audit_events = events

    class _FakeMCPSession:
        def __init__(self) -> None:
            self.call_count = 0

        async def send_progress_notification(
            self,
            *,
            progress_token: "str | int",
            progress: float,
            total: float | None = None,
            message: str | None = None,
            related_request_id: str | None = None,
        ) -> None:
            self.call_count += 1

    mcp_session = _FakeMCPSession()
    bridge = _MCPProgressBridge(
        session=_SessionWithChatEvents(),
        mcp_session=mcp_session,
        progress_token="tok-1",
        related_request_id="req-1",
    )

    async def _drive() -> int:
        bridge.attach()
        try:
            for _ in range(_N):
                events.emit("tool_returned", tool="grep", chain_id="c1")
                await _drain()
            return bridge.tracked_task_count
        finally:
            bridge.detach()

    final_count = asyncio.run(_drive())

    assert final_count < _N, (
        f"tracked_task_count ({final_count}) grew with the {_N} driven "
        "events instead of staying bounded — finished tasks are not "
        "being removed from _tasks"
    )
    assert final_count == 0
    assert mcp_session.call_count == _N


# ── 3. detach() cancellation semantics survive the removal callback ────


def test_a2a_bridge_detach_still_cancels_inflight_task_while_removal_wired() -> None:
    """Tier 2: with the done-callback removal wired in, ``detach()``
    still cancels a task that is genuinely in flight — the completion
    based removal path and the cancellation path don't race each other
    into leaving a task neither cancelled nor tracked correctly."""
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge
    from reyn.interfaces.web.run_registry import RunRegistry

    events = EventLog()

    class _SessionWithChatEvents:
        _audit_events = events

    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="demo", chain_id="c1")
    bridge = _A2AProgressBridge(
        session=_SessionWithChatEvents(),
        run_id=entry.run_id,
        webhook_url=None,
        agent_name="demo",
        run_registry=run_registry,
    )

    sent_events: list[str] = []

    async def _slow_send(ordinal: int, event_type: str, message: str) -> None:
        sent_events.append("started")
        try:
            # Never completes on its own — only detach()'s cancel() ends
            # this wait, which is exactly what the test is exercising.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sent_events.append("cancelled")
            raise
        sent_events.append("finished")

    bridge._send = _slow_send  # type: ignore[method-assign]

    async def _drive() -> None:
        bridge.attach()
        events.emit("tool_returned", tool="grep", chain_id="c1")
        await _drain(2)
        assert sent_events == ["started"]
        assert bridge.tracked_task_count == 1
        bridge.detach()
        await _drain(2)

    asyncio.run(_drive())

    assert "cancelled" in sent_events
    # The cancelled task's done callback still fires and removes it.
    assert bridge.tracked_task_count == 0
