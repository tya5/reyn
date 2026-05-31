"""Tier 2: OS invariant tests for CompactionController (FP-0019 Wave 1).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real CompactionConfig, real
  ChatMessage instances.
- No private-state assertions.  Observation flows through:
    - events.all() (EventLog public read accessor)
    - event.type / event.data (public fields on Event)
- Each test docstring's first line starts with ``Tier 2: ...``.

#1128 PR-a: the background fire-and-forget path (``spawn_maybe`` /
``_maybe_compact``) was removed; ``force_compact_now`` — the synchronous
pre-frame guard path — is the sole controller-driven compaction entry point.
Candidate selection is token-budget (step 3, ``_select_candidates`` via the
engine's ComputedBudgets head_budget/tail_budget), so the stub engines below
expose synthetic ``budgets``.
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
    ComputedBudgets,
    HistoryChunkToCompact,
)

# Synthetic budgets: head/tail each fit ~one 50-token turn ("x"*200 via chars4),
# so a 7-turn history yields head=[t1], tail=[t7], middle=[t2..t6] = candidates.
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000,
    tail_budget=50, new_msg_budget=10_000,
    B_M=80_000, main_M_room=65_000, effective_trigger=65_000,
)


@dataclass
class _FakeMessage:
    """Minimal ChatMessage substitute for controller tests."""
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)


class _AbortingEngine(CompactionEngine):
    """Engine stub that always raises so compaction aborts early (no LLM call)."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        raise RuntimeError("aborting engine stub: test-time abort")


class _SucceedingEngine(CompactionEngine):
    """Engine stub that returns a minimal ChatSummary without an LLM call."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        seqs = [int(t.get("seq", 0)) for t in input_chunk.new_turns if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _make_controller(
    *,
    history: list[_FakeMessage],
    engine: CompactionEngine,
) -> tuple[CompactionController, EventLog, list[_FakeMessage]]:
    """Return a (controller, events, history) triple ready for testing."""
    events = EventLog()

    def _latest_summary():
        for m in reversed(history):
            if m.role == "summary":
                return m
        return None

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_access=lambda: list(history),
        latest_summary=_latest_summary,
        compaction_engine=engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, events, history


def _history(n: int) -> list[_FakeMessage]:
    return [
        _FakeMessage(role="user" if i % 2 == 1 else "assistant", text="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Invariant 1: no middle candidates (small chat) → forced_sync, no compaction
# ---------------------------------------------------------------------------


def test_force_compact_no_candidates_emits_forced_sync_no_started():
    """Tier 2: when head+tail token budgets cover the whole history (no middle
    to compact), force_compact_now emits compaction_check(outcome='forced_sync')
    with candidate_count=0 and does NOT emit compaction_started.
    """
    ctrl, events, _ = _make_controller(history=_history(2), engine=_AbortingEngine())

    asyncio.run(ctrl.force_compact_now())

    emitted = events.all()
    forced = [e for e in emitted if e.type == "compaction_check"
              and e.data.get("outcome") == "forced_sync"]
    started = [e for e in emitted if e.type == "compaction_started"]
    assert forced, "expected a forced_sync compaction_check event"
    assert forced[0].data.get("candidate_count") == 0
    assert not started, "compaction_started must not fire with no candidates"


# ---------------------------------------------------------------------------
# Invariant 2: middle candidates present → compaction runs + summary appended
# ---------------------------------------------------------------------------


def test_force_compact_with_candidates_appends_summary():
    """Tier 2: with a compactable middle, force_compact_now runs the engine
    (compaction_started + compaction_completed) and appends a summary entry.
    """
    ctrl, events, hist = _make_controller(history=_history(7), engine=_SucceedingEngine())

    asyncio.run(ctrl.force_compact_now())

    emitted = events.all()
    assert [e for e in emitted if e.type == "compaction_started"], "expected compaction_started"
    assert [e for e in emitted if e.type == "compaction_completed"], "expected compaction_completed"
    summaries = [m for m in hist if m.role == "summary"]
    assert summaries, "force_compact_now must append a summary entry on success"


# ---------------------------------------------------------------------------
# Invariant 3: engine failure → compaction_failed emitted, no raise to caller
# ---------------------------------------------------------------------------


def test_force_compact_engine_failure_emits_failed():
    """Tier 2: when the engine raises mid-compaction, force_compact_now emits
    compaction_failed and returns (the try/except swallows the engine error
    rather than propagating it to the caller)."""
    ctrl, events, _ = _make_controller(history=_history(7), engine=_AbortingEngine())

    asyncio.run(ctrl.force_compact_now())  # must not raise

    assert [e for e in events.all() if e.type == "compaction_failed"], (
        "engine failure during force_compact_now must emit compaction_failed"
    )
