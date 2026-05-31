"""Tier 2: OS invariant tests for CompactionController (FP-0019 Wave 1).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real CompactionConfig, real
  ChatMessage instances.
- No private-state assertions.  Observation flows through:
    - events.all() (EventLog public read accessor)
    - event.type / event.data (public fields on Event)
- Each test docstring's first line starts with ``Tier 2: ...``.

#1128 PR-a: the background fire-and-forget path (``spawn_maybe`` /
``_maybe_compact``, gated by ``trigger_total_tokens`` / ``min_compact_batch``)
was removed. ``force_compact_now`` — the synchronous pre-frame guard path —
is now the sole controller-driven compaction entry point; these invariants
pin its candidate-selection + event contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest  # noqa: F401 — used implicitly by pytest discovery

from reyn.chat.services.compaction_controller import CompactionController
from reyn.config import CompactionConfig
from reyn.events.events import EventLog
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    HistoryChunkToCompact,
)

# ---------------------------------------------------------------------------
# Helpers — minimal ChatMessage stand-in and engine stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    """Minimal ChatMessage substitute for controller tests."""
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)


class _AbortingEngine(CompactionEngine):
    """Engine stub that always raises so compaction aborts early.

    Inherits from the real engine but overrides compact() — no LLM call.
    """

    def __init__(self) -> None:
        # Skip CompactionEngine.__init__ (needs model + events).
        self._model = "stub"
        self._events = EventLog()

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        raise RuntimeError("aborting engine stub: test-time abort")


class _SucceedingEngine(CompactionEngine):
    """Engine stub that returns a minimal ChatSummary without an LLM call."""

    def __init__(self, covers: int = 0) -> None:
        self._model = "stub"
        self._events = EventLog()
        self._covers = covers

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        seqs = [int(t.get("seq", 0)) for t in input_chunk.new_turns if isinstance(t, dict)]
        covers = max(seqs) if seqs else self._covers
        return ChatSummary(topic_arc="stub", covers_through_seq=covers)


def _make_controller(
    *,
    history: list[_FakeMessage] | None = None,
    config: CompactionConfig | None = None,
    engine: CompactionEngine | None = None,
) -> tuple[CompactionController, EventLog, list[_FakeMessage]]:
    """Return a (CompactionController, EventLog, history) triple for testing."""
    events = EventLog()
    _history: list[_FakeMessage] = history if history is not None else []
    cfg = config or CompactionConfig(head_size=2, tail_size=2)

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
        compaction_engine=engine or _AbortingEngine(),
        history_appender=_appender,
        make_summary_message=_make_msg,
        render_summary=lambda s: str(s),
    )
    return ctrl, events, _history


# ---------------------------------------------------------------------------
# Invariant 1: no candidates (all turns within head+tail) → no compaction
# ---------------------------------------------------------------------------


def test_force_compact_no_candidates_emits_forced_sync_no_started():
    """Tier 2: when every turn falls inside the HEAD/TAIL window (no middle
    to compact), force_compact_now emits compaction_check(outcome='forced_sync')
    with candidate_count=0 and does NOT emit compaction_started.

    head=2, tail=2 with 4 turns → cover_floor=2, tail_threshold=max_seq-2, so
    the candidate set (cover_floor < seq <= tail_threshold) is empty.
    """
    history: list[_FakeMessage] = []
    for i in range(1, 5):  # seq 1..4
        role = "user" if i % 2 == 1 else "assistant"
        history.append(_FakeMessage(role=role, text="hi", seq=i))

    ctrl, events, _ = _make_controller(
        history=history,
        config=CompactionConfig(head_size=2, tail_size=2),
    )

    asyncio.run(ctrl.force_compact_now())

    emitted = events.all()
    forced = [
        e for e in emitted
        if e.type == "compaction_check" and e.data.get("outcome") == "forced_sync"
    ]
    started = [e for e in emitted if e.type == "compaction_started"]
    assert forced, "expected a forced_sync compaction_check event"
    assert forced[0].data.get("candidate_count") == 0
    assert not started, (
        f"compaction_started must not fire with no candidates, got "
        f"{[e.data for e in started]}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: candidates present → compaction runs + summary appended
# ---------------------------------------------------------------------------


def test_force_compact_with_candidates_appends_summary():
    """Tier 2: with a compactable middle, force_compact_now runs the engine
    (compaction_started + compaction_completed) and appends a summary entry to
    history. Uses a succeeding engine stub — no LLM call.

    head=2, tail=2 with 7 turns → candidates are seq 3,4,5.
    """
    history: list[_FakeMessage] = []
    for i in range(1, 8):  # seq 1..7
        role = "user" if i % 2 == 1 else "assistant"
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    ctrl, events, hist = _make_controller(
        history=history,
        config=CompactionConfig(head_size=2, tail_size=2),
        engine=_SucceedingEngine(),
    )

    asyncio.run(ctrl.force_compact_now())

    emitted = events.all()
    started = [e for e in emitted if e.type == "compaction_started"]
    completed = [e for e in emitted if e.type == "compaction_completed"]
    assert started, "expected compaction_started"
    assert completed, "expected compaction_completed"
    # A summary entry was appended (covers through the last candidate, seq 5).
    summaries = [m for m in hist if m.role == "summary"]
    assert summaries, "force_compact_now must append a summary entry on success"
    assert summaries[-1].meta.get("covers_through_seq") == 5


# ---------------------------------------------------------------------------
# Invariant 3: engine failure → compaction_failed emitted, no raise to caller
# ---------------------------------------------------------------------------


def test_force_compact_engine_failure_emits_failed():
    """Tier 2: when the engine raises mid-compaction, force_compact_now emits
    compaction_failed and returns (the per-pass try/except swallows the engine
    error rather than propagating it to the caller)."""
    history: list[_FakeMessage] = []
    for i in range(1, 8):
        role = "user" if i % 2 == 1 else "assistant"
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    ctrl, events, _ = _make_controller(
        history=history,
        config=CompactionConfig(head_size=2, tail_size=2),
        engine=_AbortingEngine(),
    )

    asyncio.run(ctrl.force_compact_now())  # must not raise

    emitted = events.all()
    failed = [e for e in emitted if e.type == "compaction_failed"]
    assert failed, "engine failure during force_compact_now must emit compaction_failed"
