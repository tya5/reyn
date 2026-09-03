"""Tier 2: OS invariant tests for CompactionController (FP-0019 Wave 1).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real CompactionConfig, real
  ChatMessage instances.
- No private-state assertions.  Observation flows through:
    - collect_events(event_log) (tests/_support/events.py — a live subscriber
      list, the same mechanism production's EventStore subscriber uses)
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
from typing import Callable

import pytest  # noqa: F401 — used implicitly by pytest discovery

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import (
    SUMMARY_MESSAGE_ROLE,
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    CoversThrough,
    HistoryChunkToCompact,
)
from tests._support.events import collect_events, settle


async def _run_and_settle(coro, log):
    result = await coro
    await settle(log)
    return result

# Synthetic budgets: head/tail each fit ~one 50-token turn ("x"*200 via chars4),
# so a 7-turn history yields head=[t1], tail=[t7], middle=[t2..t6] = candidates.
# #5719: main_M_room=0 — these tests are about is_compacting/failure/summary-
# append behavior, not the shortfall-selection algorithm itself (that has its
# own dedicated tests), so main_M_room is set to always produce a shortfall
# for any nonempty middle, matching the pre-#5719 "select everything between
# head and tail" shape byte-for-byte for this file's own fixtures.
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000,
    tail_budget=50, new_msg_budget=10_000,
    B_M=80_000, main_M_room=0, effective_trigger=0,
)


def _emit_compaction_started(
    events: EventLog, input_chunk: HistoryChunkToCompact, covers_through: CoversThrough,
) -> None:
    """#5475: mirrors ``CompactionEngine.compact()``'s own real entry-point
    emit — the stub engines below call this instead of a real ``compact()``
    body, so this test file's own ``compaction_started`` witnesses stay
    meaningful (the SAME shape production now emits, not a shape this file
    invented independently that could silently drift from it)."""
    # #5531: new_turn_count/had_previous mirror CompactionEngine.compact()'s
    # own derivation — a "summary" element (at most one) is not a "new
    # turn" being summarised for the first time.
    _summary_messages = [
        m for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
    ]
    events.emit(
        "compaction_started",
        new_turn_count=len(input_chunk.messages) - len(_summary_messages),
        covers_through_seq=covers_through if isinstance(covers_through, int) else None,
        covers_through_unavailable_reason=(
            None if isinstance(covers_through, int) else covers_through.value
        ),
        had_previous=bool(_summary_messages),
    )


class _AbortingEngine(CompactionEngine):
    """Engine stub that always raises so compaction aborts early (no LLM call).

    #5475: takes the SAME ``EventLog`` the controller/test observe (not a
    private, disconnected one) — ``compaction_started`` now emits inside
    ``compact()``, not the controller, so a stub whose own event log nobody
    watches would silently stop producing that event for every test here."""

    def __init__(self, events: EventLog) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _emit_compaction_started(self._events, input_chunk, covers_through)
        # #5633: mirrors CompactionEngine.compact()'s own real except —
        # compaction_failed now emits from compact() itself (the same
        # "one real entry both callers share" argument #5475 already made
        # for compaction_started), so a stub whose own event log nobody
        # watches would otherwise silently stop producing that event too.
        error = RuntimeError("aborting engine stub: test-time abort")
        self._events.emit("compaction_failed", error=str(error))
        raise error


class _SucceedingEngine(CompactionEngine):
    """Engine stub that returns a minimal ChatSummary without an LLM call."""

    def __init__(self, events: EventLog) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _emit_compaction_started(self._events, input_chunk, covers_through)
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _make_controller(
    *,
    history: list[ChatMessage],
    engine_factory: "Callable[[EventLog], CompactionEngine]",
) -> tuple[CompactionController, list, list[ChatMessage], EventLog]:
    """Return a (controller, collected, history, events) tuple ready for testing.

    #5475: takes an ``engine_factory(events)`` rather than a pre-built
    ``engine`` — the stub engines need the SAME ``events`` this function
    builds below (their own ``compaction_started`` now lives inside
    ``compact()``, see ``_emit_compaction_started`` above), which does not
    exist yet at the caller's own call site."""
    events = EventLog()
    collected = collect_events(events)
    engine = engine_factory(events)

    def _latest_summary():
        for m in reversed(history):
            if m.role == "summary":
                return m
        return None

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        # #4472: history_from_disk(after_seq) — this suite's `history` list
        # already stands in for "the durable source of truth" (it's the
        # ONLY source these unit tests ever construct), so the real
        # disk-reading mechanism itself is out of scope here (covered by
        # tests/runtime/test_4472_compaction_reads_durable_store.py against
        # a real Session + real history.jsonl instead) — this just narrows
        # the same list by seq, matching the real method's contract.
        # (list, truncated=False) -- these unit tests never exercise the
        # #4472 batch cap itself (covered end-to-end by
        # test_4472_compaction_reads_durable_store.py instead).
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=_latest_summary,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, collected, history, events


def _history(n: int) -> list[ChatMessage]:
    # #2957 PR-A: real ChatMessage (not a hand-rolled substitute) — a prior
    # substitute stored the turn text under a stray ``text`` field with no
    # ``content`` attribute at all, which only produced "large turns" via
    # the now-fixed estimate_tokens_for_turn/ChatMessage type-mismatch bug's
    # getattr(turn, "content", None) -> None fallback. Real ChatMessage has
    # no such field — content IS the text, and .text is derived from it.
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
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
    ctrl, collected, _, events = _make_controller(history=_history(2), engine_factory=_AbortingEngine)

    asyncio.run(_run_and_settle(ctrl.force_compact_now(spill_fn=lambda _candidates: []), events))

    emitted = collected
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
    ctrl, collected, hist, events = _make_controller(history=_history(7), engine_factory=_SucceedingEngine)

    asyncio.run(_run_and_settle(ctrl.force_compact_now(spill_fn=lambda _candidates: []), events))

    emitted = collected
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
    ctrl, collected, _, events = _make_controller(history=_history(7), engine_factory=_AbortingEngine)

    asyncio.run(_run_and_settle(ctrl.force_compact_now(spill_fn=lambda _candidates: []), events))  # must not raise

    assert [e for e in collected if e.type == "compaction_failed"], (
        "engine failure during force_compact_now must emit compaction_failed"
    )


# ---------------------------------------------------------------------------
# Invariant 3b (#5633, lead-coder BLOCKING on PR #5661): a failure BEFORE
# compact() is ever called must still emit compaction_failed
# ---------------------------------------------------------------------------


def test_force_compact_pre_compact_setup_failure_emits_failed():
    """Tier 2: #5633 — a failure in _run_compaction's OWN setup segment
    (building prev_structured/the input_chunk, before compact() is ever
    called) must still emit compaction_failed. Before #5633's first
    revision, force_compact_now's own catch-all emitted for ANY failure in
    _run_compaction, including this segment; the first #5633 restructure
    (compact() outside any try, post-processing in its own try) left this
    EARLIER segment in neither try — a real observability regression
    caught by lead-coder's own review, not a hypothetical.

    Triggers a REAL exception in real production code (render_summary_
    for_storage's own ``.strip()`` on a non-string ``topic_arc`` — no
    mock/stand-in): a genuinely malformed persisted summary (the kind a
    corrupted history.jsonl could contain) is exactly the kind of input
    this segment must not silently swallow. compact() must never be
    reached (compaction_started absent) — proves this is THIS segment's
    own except firing, not the compact()-internal or post-processing
    one."""
    history = [
        ChatMessage(
            role="summary", content="stale", seq=0,
            meta={"structured": {"topic_arc": 12345}},  # not a str -> .strip() raises
        ),
        *_history(7),
    ]
    ctrl, collected, _, events = _make_controller(history=history, engine_factory=_SucceedingEngine)

    asyncio.run(_run_and_settle(ctrl.force_compact_now(spill_fn=lambda _candidates: []), events))  # must not raise

    kinds = [e.type for e in collected]
    assert "compaction_started" not in kinds, (
        f"test setup sanity: compact() must never be reached for this "
        f"segment's own except to be the one under test — got: {kinds!r}"
    )
    assert "compaction_failed" in kinds, (
        "a failure in _run_compaction's own pre-compact() setup segment "
        f"must still emit compaction_failed — got: {kinds!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 4: is_compacting (#5588) — True only while a pass is in flight
# ---------------------------------------------------------------------------


class _ObservingEngine(CompactionEngine):
    """#5588: like ``_SucceedingEngine``, but captures ``ctrl.is_compacting``
    from INSIDE its own ``compact()`` call — the only way to observe the
    flag's value DURING a pass from a test that otherwise only sees before/
    after force_compact_now's single await chain."""

    def __init__(self, events: EventLog, ctrl_holder: dict) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS
        self._ctrl_holder = ctrl_holder
        self.observed_during_compact: "bool | None" = None

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _emit_compaction_started(self._events, input_chunk, covers_through)
        self.observed_during_compact = self._ctrl_holder["ctrl"].is_compacting
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def test_is_compacting_true_only_during_a_pass():
    """Tier 2: #5588 — CompactionController.is_compacting reads False before
    force_compact_now runs, True from inside the engine's own compact() call
    (observed via a real, not simulated, mid-call read), and False again
    once force_compact_now has returned — the exact signal the shrink-flow
    progress chrome row gates its own visibility on."""
    ctrl_holder: dict = {}
    engine_box: list = []

    def _factory(events: EventLog) -> CompactionEngine:
        engine = _ObservingEngine(events, ctrl_holder)
        engine_box.append(engine)
        return engine

    ctrl, _collected, _hist, events = _make_controller(history=_history(7), engine_factory=_factory)
    ctrl_holder["ctrl"] = ctrl

    assert ctrl.is_compacting is False, "must read False before any pass starts"

    asyncio.run(_run_and_settle(ctrl.force_compact_now(spill_fn=lambda _candidates: []), events))

    (engine,) = engine_box
    assert engine.observed_during_compact is True, (
        "is_compacting must read True from inside the engine's own compact() call"
    )
    assert ctrl.is_compacting is False, "must read False again once the pass has returned"
