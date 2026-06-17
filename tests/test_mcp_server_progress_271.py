"""Tier 2: Reyn-as-MCP-server progress emit + cancellation receipt
(issue #271 M1 + M2).

Pins the server-side outbound notification mechanism that #271 added:

  - M1 progress emit: when the MCP client sets ``_meta.progressToken``
    on a ``send_to_agent`` call, the server's ``_call_tool`` handler
    subscribes a bridge to the agent's chat_events EventLog and
    forwards M1-b lifecycle events as ``notifications/progress``:
      * ``phase_started`` → message="phase: <name>"
      * ``llm_called`` → message="llm: <model>"
      * ``act_executed`` → message="act: <N> op(s)"
    Each notification carries a monotonic ordinal ``progress`` (=
    no meaningful ``total``, indeterminate) so the client renders
    as a counter or spinner.

  - M2 cancellation receipt: the MCP SDK propagates client-sent
    ``CancelledNotification`` as ``anyio.CancelledError`` (=
    ``asyncio.CancelledError`` compatible) into the handler. The
    handler must re-raise so the SDK's suppression kicks in. The
    bridge teardown still runs via ``finally``.

Pins (mostly via the ``_MCPProgressBridge`` unit + the
``_make_mcp_progress_bridge`` factory; the full integration through
``send_to_agent_impl`` is too heavy for Tier 2 — we verify the
bridge's contract directly):

  1. Bridge filter: only the 3 tracked event types translate; other
     events are ignored.
  2. Bridge ordinal: monotonic, starts at 1.
  3. Bridge message format: matches the documented shape for each
     tracked event type.
  4. Bridge detach: removes the subscriber so no leak across calls.
  5. Bridge detach is idempotent (= multiple calls safe).
  6. Bridge tolerates send_progress_notification raising (= best-effort).
  7. EventLog.remove_subscriber returns False when subscriber not found.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("mcp", reason="mcp not installed")

from reyn.core.events.events import EventLog  # noqa: E402
from reyn.mcp.server import _MCPProgressBridge  # noqa: E402
from reyn.schemas.models import Event  # noqa: E402


class _FakeSession:
    """Minimal stand-in for ChatSession exposing `_chat_events`."""

    def __init__(self, event_log: EventLog) -> None:
        self._chat_events = event_log


class _FakeMCPSession:
    """Captures send_progress_notification calls so tests can assert
    the bridge's output shape without a real MCP transport.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def send_progress_notification(
        self,
        *,
        progress_token: str | int,
        progress: float,
        total: float | None = None,
        message: str | None = None,
        related_request_id: str | None = None,
    ) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls.append({
            "progress_token": progress_token,
            "progress": progress,
            "total": total,
            "message": message,
            "related_request_id": related_request_id,
        })


def _make_bridge(
    *,
    progress_token: str | int = "tok-1",
    related_request_id: str | None = "req-1",
) -> tuple[_MCPProgressBridge, EventLog, _FakeMCPSession]:
    event_log = EventLog()
    fake_chat = _FakeSession(event_log)
    fake_mcp = _FakeMCPSession()
    bridge = _MCPProgressBridge(
        session=fake_chat,
        mcp_session=fake_mcp,
        progress_token=progress_token,
        related_request_id=related_request_id,
    )
    bridge.attach()
    return bridge, event_log, fake_mcp


# ── 1. Tracked event filter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_forwards_phase_started_as_progress_notification() -> None:
    """Tier 2: ``phase_started`` event becomes a progress notification
    with message ``phase: <name>``.
    """
    bridge, event_log, fake_mcp = _make_bridge()
    try:
        event_log.emit("phase_started", phase="greet")
        # Yield to let the spawned task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake_mcp.calls, "expected at least one progress notification"
        (call,) = fake_mcp.calls
        assert call["progress_token"] == "tok-1"
        assert call["progress"] == 1.0
        assert call["total"] is None
        assert call["message"] == "phase: greet"
        assert call["related_request_id"] == "req-1"
    finally:
        bridge.detach()


@pytest.mark.asyncio
async def test_bridge_forwards_llm_called_with_model_message() -> None:
    """Tier 2: ``llm_called`` event becomes ``llm: <model>``."""
    bridge, event_log, fake_mcp = _make_bridge()
    try:
        event_log.emit("llm_called", model="gemini-2.5-flash-lite")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake_mcp.calls[-1]["message"] == "llm: gemini-2.5-flash-lite"
    finally:
        bridge.detach()


@pytest.mark.asyncio
async def test_bridge_forwards_act_executed_with_op_count() -> None:
    """Tier 2: ``act_executed`` event becomes ``act: <N> op(s)`` with
    correct singular / plural.
    """
    bridge, event_log, fake_mcp = _make_bridge()
    try:
        event_log.emit("act_executed", op_count=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake_mcp.calls[-1]["message"] == "act: 1 op"

        event_log.emit("act_executed", op_count=3)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake_mcp.calls[-1]["message"] == "act: 3 ops"
    finally:
        bridge.detach()


@pytest.mark.asyncio
async def test_bridge_ignores_non_tracked_events() -> None:
    """Tier 2: events outside the M1-b tracked set don't fire any
    progress notification. Keeps the channel useful (= no noise from
    high-volume / low-info events like ``llm_response_received``).
    """
    bridge, event_log, fake_mcp = _make_bridge()
    try:
        event_log.emit("llm_response_received", model="x")
        event_log.emit("phase_completed", phase="greet")
        event_log.emit("workflow_finished")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake_mcp.calls == []
    finally:
        bridge.detach()


# ── 2. Monotonic ordinal ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_ordinal_increases_monotonically() -> None:
    """Tier 2: ``progress`` is a monotonic ordinal counter starting at
    1.0, not a fraction. ``total=None`` always (= indeterminate).
    """
    bridge, event_log, fake_mcp = _make_bridge()
    try:
        event_log.emit("phase_started", phase="a")
        event_log.emit("llm_called", model="m")
        event_log.emit("act_executed", op_count=2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        progressions = [c["progress"] for c in fake_mcp.calls]
        assert progressions == [1.0, 2.0, 3.0]
        assert all(c["total"] is None for c in fake_mcp.calls)
    finally:
        bridge.detach()


# ── 3. Detach + leak prevention ────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_detach_removes_event_subscriber() -> None:
    """Tier 2: after ``detach()``, subsequent events on the same
    EventLog don't trigger notifications — the subscriber is gone.

    This is the leak-prevention guarantee that makes per-call bridge
    instances safe (= no growing subscriber list across many MCP
    tool calls).
    """
    bridge, event_log, fake_mcp = _make_bridge()
    bridge.detach()
    event_log.emit("phase_started", phase="greet")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert fake_mcp.calls == []


@pytest.mark.asyncio
async def test_bridge_detach_is_idempotent() -> None:
    """Tier 2: calling ``detach()`` twice doesn't raise. Defensive
    against double-cleanup paths (= e.g. both the try/except and the
    finally calling detach in some refactor).
    """
    bridge, _event_log, _fake_mcp = _make_bridge()
    bridge.detach()
    bridge.detach()  # MUST NOT raise


# ── 4. Resilience to transport failures ────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_swallows_send_progress_notification_errors() -> None:
    """Tier 2: when ``send_progress_notification`` raises (= transport
    failure, peer disconnect, etc.), the bridge swallows the error.

    Per #271 owner-decision: "progress is best-effort; the main call
    must never fail because we couldn't push a notification". The
    skill execution continues unaffected.
    """
    bridge, event_log, fake_mcp = _make_bridge()
    fake_mcp.raise_on_call = RuntimeError("transport gone")
    try:
        event_log.emit("phase_started", phase="x")
        # No exception should propagate to the test.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let any task settle
        # Bridge is still attached + functional for next event (= no
        # poisoning from earlier failure).
        fake_mcp.raise_on_call = None
        event_log.emit("phase_started", phase="y")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # The second event was successfully forwarded.
        success_calls = [c for c in fake_mcp.calls if c["message"] == "phase: y"]
        assert success_calls, "expected the second event to be forwarded"
    finally:
        bridge.detach()


# ── 5. EventLog.remove_subscriber ──────────────────────────────────────


def test_event_log_remove_subscriber_returns_false_when_unknown() -> None:
    """Tier 2: ``EventLog.remove_subscriber`` on an unknown fn returns
    False instead of raising. Lets defensive cleanup paths (= bridge
    detach when attach failed silently) be safe to call.
    """
    log = EventLog()

    def fn(event: Event) -> None:
        return None

    assert log.remove_subscriber(fn) is False


def test_event_log_remove_subscriber_returns_true_when_removed() -> None:
    """Tier 2: ``remove_subscriber`` returns True when the fn was
    actually in the list + has been removed.
    """
    log = EventLog()

    def fn(event: Event) -> None:
        return None

    log.add_subscriber(fn)
    assert log.remove_subscriber(fn) is True
    # Second remove returns False.
    assert log.remove_subscriber(fn) is False


# ── 6. M2 cancellation receipt (= integration test) ────────────────────


@pytest.mark.asyncio
async def test_bridge_detach_cancels_inflight_notification_tasks() -> None:
    """Tier 2: when the handler is cancelled mid-notification, the
    bridge's ``detach()`` cancels any still-pending notification tasks.

    This is the M2 cancellation receipt path: SDK propagates
    CancelledError to the handler, handler's finally runs detach,
    detach cancels in-flight notification dispatches. Prevents
    notification tasks from outliving the request.
    """
    bridge, event_log, fake_mcp = _make_bridge()

    # Simulate a slow notification by replacing _send with a long-await.
    sent_events: list[str] = []

    async def _slow_send(progress: float, message: str) -> None:
        sent_events.append("started")
        try:
            await asyncio.sleep(5.0)  # long enough to be cancelled
        except asyncio.CancelledError:
            sent_events.append("cancelled")
            raise
        sent_events.append("finished")

    bridge._send = _slow_send  # type: ignore[method-assign]

    event_log.emit("phase_started", phase="x")
    # Let the slow send start.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sent_events == ["started"]

    # Detach should cancel the in-flight task.
    bridge.detach()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The slow send saw the cancellation.
    assert "cancelled" in sent_events
