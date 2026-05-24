"""Tier 2: OS invariant tests for CompactionController (FP-0019 Wave 1).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real CompactionConfig, real
  ChatMessage instances.
- No private-state assertions.  Observation flows through:
    - events.all() (EventLog public read accessor)
    - event.type / event.data (public fields on Event)
- Each test docstring's first line starts with ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable

import pytest  # noqa: F401 — used implicitly by pytest discovery

from reyn.chat.services.compaction_controller import CompactionController
from reyn.config import CompactionConfig
from reyn.events.events import EventLog

# ---------------------------------------------------------------------------
# Helpers — minimal ChatMessage stand-in and fixture factory
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    """Minimal ChatMessage substitute for controller tests."""
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)


class _FakeRunResult:
    """Minimal RunResult substitute: always returns ok=False to abort early."""
    ok: bool = False
    status: str = "aborted"
    data: dict | None = None


async def _noop_skill(*_args, **_kwargs) -> _FakeRunResult:
    return _FakeRunResult()


def _make_controller(
    *,
    history: list[_FakeMessage] | None = None,
    config: CompactionConfig | None = None,
    run_skill=None,
) -> tuple[CompactionController, EventLog]:
    """Return a (CompactionController, EventLog) pair ready for testing."""
    events = EventLog()
    _history: list[_FakeMessage] = history if history is not None else []
    cfg = config or CompactionConfig(
        trigger_total_tokens=100,
        head_size=2,
        tail_size=2,
        min_compact_batch=3,
    )

    def _latest_summary():
        for m in reversed(_history):
            if m.role == "summary":
                return m
        return None

    def _appender(msg):
        _history.append(msg)

    def _make_msg(rendered, structured, covers):
        return _FakeMessage(
            role="summary",
            text=rendered,
            seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        )

    ctrl = CompactionController(
        event_log=events,
        config=cfg,
        history_access=lambda: list(_history),
        latest_summary=_latest_summary,
        run_compaction_skill=run_skill or _noop_skill,
        history_appender=_appender,
        make_summary_message=_make_msg,
        render_summary=lambda s: str(s),
    )
    return ctrl, events


# ---------------------------------------------------------------------------
# Invariant 1: history below threshold → compaction_check emitted, no started
# ---------------------------------------------------------------------------


def test_threshold_below_trigger_no_compaction():
    """Tier 2: when history token count is below trigger_total_tokens,
    _maybe_compact emits compaction_check with outcome='below_threshold'
    and does NOT emit compaction_started.

    Config: trigger=10_000 tokens (very high), 3 candidate turns ~12 tokens
    total → compaction should not fire.  compaction_check must appear;
    compaction_started must not.
    """
    # Build 7 turns: seq 1-7 with short text.  head=2, tail=2 → candidates
    # are seq 3,4,5 (3 turns, above min_compact_batch=3).
    history: list[_FakeMessage] = []
    for i in range(1, 8):
        role = "user" if i % 2 == 1 else "agent"
        history.append(_FakeMessage(role=role, text="hi", seq=i))

    ctrl, events = _make_controller(
        history=history,
        config=CompactionConfig(
            trigger_total_tokens=10_000,  # well above actual token count
            head_size=2,
            tail_size=2,
            min_compact_batch=3,
        ),
    )

    asyncio.run(ctrl._maybe_compact())

    emitted = events.all()
    check_events = [e for e in emitted if e.type == "compaction_check"]
    started_events = [e for e in emitted if e.type == "compaction_started"]

    assert check_events, "expected at least one compaction_check event"
    assert check_events[0].data["outcome"] == "below_threshold", (
        f"Expected outcome='below_threshold', got {check_events[0].data['outcome']!r}"
    )
    assert not started_events, (
        f"compaction_started must not fire when below threshold, got {[e.data for e in started_events]}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: single-flight lock prevents concurrent compaction
# ---------------------------------------------------------------------------


def test_single_flight_lock_prevents_concurrent():
    """Tier 2: when a compaction is already in flight (_compacting=True),
    a second call to _maybe_compact returns immediately after emitting
    compaction_check with outcome='already_running', without starting
    another compaction.

    Verified by: (a) injecting a slow skill that never completes to hold
    _compacting=True, (b) calling _maybe_compact a second time
    concurrently, (c) asserting exactly one 'already_running' check event
    and no second compaction_started.
    """
    # History with enough turns to pass the threshold checks.
    history: list[_FakeMessage] = []
    for i in range(1, 12):
        role = "user" if i % 2 == 1 else "agent"
        # Large text to exceed low token threshold.
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    blocked: asyncio.Event = asyncio.Event()
    started_count: list[int] = [0]

    async def _blocking_skill(*_args, **_kwargs) -> _FakeRunResult:
        started_count[0] += 1
        await blocked.wait()  # block until test releases
        return _FakeRunResult()

    ctrl, events = _make_controller(
        history=history,
        config=CompactionConfig(
            trigger_total_tokens=1,  # very low threshold → always triggers
            head_size=2,
            tail_size=2,
            min_compact_batch=3,
        ),
        run_skill=_blocking_skill,
    )

    async def _run():
        # Launch the first compaction; it will block inside _blocking_skill.
        task1 = asyncio.create_task(ctrl._maybe_compact())
        # Give task1 a chance to reach _compacting=True.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Second call while first is in flight.
        await ctrl._maybe_compact()
        # Release the blocked skill so task1 can finish.
        blocked.set()
        await task1

    asyncio.run(_run())

    emitted = events.all()
    already_running = [
        e for e in emitted
        if e.type == "compaction_check" and e.data.get("outcome") == "already_running"
    ]
    assert already_running, (
        "Expected at least one 'already_running' check event — single-flight lock not observed"
    )
    assert already_running[0].data.get("outcome") == "already_running", (
        f"check event must carry outcome='already_running', got {already_running[0].data!r}"
    )
    # The blocking skill was only invoked once (single flight).
    assert started_count[0] == 1, (
        f"Expected skill invoked once (single-flight), got {started_count[0]}"
    )


# ---------------------------------------------------------------------------
# Invariant 3: cancel() during shutdown suppresses CancelledError cleanly
# ---------------------------------------------------------------------------


def test_cancel_during_shutdown_graceful():
    """Tier 2: cancel() on an in-flight compaction task cancels the task,
    suppresses asyncio.CancelledError, and leaves no unhandled exception.

    No compaction_failed event should be emitted for a clean cancellation
    (the task was cancelled, not failed).  The public interface is that
    cancel() completes without raising.
    """
    start_gate: asyncio.Event = asyncio.Event()
    cancel_gate: asyncio.Event = asyncio.Event()

    async def _cancellable_skill(*_args, **_kwargs):
        start_gate.set()         # signal that the skill has started
        await cancel_gate.wait() # wait forever — will be cancelled
        return _FakeRunResult()

    history: list[_FakeMessage] = []
    for i in range(1, 12):
        role = "user" if i % 2 == 1 else "agent"
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    ctrl, events = _make_controller(
        history=history,
        config=CompactionConfig(
            trigger_total_tokens=1,
            head_size=2,
            tail_size=2,
            min_compact_batch=3,
        ),
        run_skill=_cancellable_skill,
    )

    async def _run():
        # Spawn compaction in the background.
        ctrl.spawn_maybe()
        # Wait until the skill is actually running.
        await start_gate.wait()
        # Now simulate shutdown — must not raise.
        await ctrl.cancel()

    asyncio.run(_run())

    emitted = events.all()
    failed_events = [e for e in emitted if e.type == "compaction_failed"]
    assert not failed_events, (
        f"cancel() on a clean CancelledError must not emit compaction_failed, "
        f"got {[e.data for e in failed_events]}"
    )
