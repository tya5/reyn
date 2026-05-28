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

import pytest  # noqa: F401 — used implicitly by pytest discovery

from reyn.chat.services.chat_compaction_engine import (
    ChatCompactionEngine,
    ChatSummary,
    HistoryChunkToCompact,
)
from reyn.chat.services.compaction_controller import CompactionController
from reyn.config import CompactionConfig
from reyn.events.events import EventLog

# ---------------------------------------------------------------------------
# Helpers — minimal ChatMessage stand-in and engine stub
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    """Minimal ChatMessage substitute for controller tests."""
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)


class _AbortingEngine(ChatCompactionEngine):
    """Engine stub that always raises so compaction aborts early.

    Inherits from the real engine but overrides compact() — no LLM call.
    """

    def __init__(self) -> None:
        # Skip ChatCompactionEngine.__init__ (needs model + events).
        self._model = "stub"
        self._events = EventLog()

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        raise RuntimeError("aborting engine stub: test-time abort")


class _SucceedingEngine(ChatCompactionEngine):
    """Engine stub that returns a minimal ChatSummary without an LLM call."""

    def __init__(self, covers: int = 0) -> None:
        self._model = "stub"
        self._events = EventLog()
        self._covers = covers

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        seqs = [int(t.get("seq", 0)) for t in input_chunk.new_turns if isinstance(t, dict)]
        covers = max(seqs) if seqs else self._covers
        return ChatSummary(topic_arc="stub", covers_through_seq=covers)


class _BlockingEngine(ChatCompactionEngine):
    """Engine stub that blocks on an asyncio.Event — for single-flight tests."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._model = "stub"
        self._events = EventLog()
        self._gate = gate
        self.call_count = 0

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        self.call_count += 1
        await self._gate.wait()
        raise RuntimeError("blocking engine stub released")


class _CancellableEngine(ChatCompactionEngine):
    """Engine stub that signals start then waits forever (for cancel tests)."""

    def __init__(self, start_gate: asyncio.Event, cancel_gate: asyncio.Event) -> None:
        self._model = "stub"
        self._events = EventLog()
        self._start = start_gate
        self._cancel = cancel_gate

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        self._start.set()
        await self._cancel.wait()
        return ChatSummary(topic_arc="", covers_through_seq=0)


def _make_controller(
    *,
    history: list[_FakeMessage] | None = None,
    config: CompactionConfig | None = None,
    engine: ChatCompactionEngine | None = None,
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
        chat_compaction_engine=engine or _AbortingEngine(),
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

    Verified by: (a) injecting a blocking engine to hold _compacting=True,
    (b) calling _maybe_compact a second time concurrently,
    (c) asserting exactly one 'already_running' check event and no second
    compaction_started.
    """
    history: list[_FakeMessage] = []
    for i in range(1, 12):
        role = "user" if i % 2 == 1 else "agent"
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    blocked: asyncio.Event = asyncio.Event()
    blocking_engine = _BlockingEngine(gate=blocked)

    ctrl, events = _make_controller(
        history=history,
        config=CompactionConfig(
            trigger_total_tokens=1,
            head_size=2,
            tail_size=2,
            min_compact_batch=3,
        ),
        engine=blocking_engine,
    )

    async def _run():
        task1 = asyncio.create_task(ctrl._maybe_compact())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await ctrl._maybe_compact()
        blocked.set()
        try:
            await task1
        except Exception:
            pass

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
    assert blocking_engine.call_count == 1, (
        f"Expected engine compact() invoked once (single-flight), got {blocking_engine.call_count}"
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
    cancellable_engine = _CancellableEngine(start_gate=start_gate, cancel_gate=cancel_gate)

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
        engine=cancellable_engine,
    )

    async def _run():
        ctrl.spawn_maybe()
        await start_gate.wait()
        await ctrl.cancel()

    asyncio.run(_run())

    emitted = events.all()
    failed_events = [e for e in emitted if e.type == "compaction_failed"]
    assert not failed_events, (
        f"cancel() on a clean CancelledError must not emit compaction_failed, "
        f"got {[e.data for e in failed_events]}"
    )
