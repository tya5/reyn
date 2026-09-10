"""Tier 2: #5939 PR-4 — `CompactionController.force_compact_now` refuses
(outcome="history_load_unsafe") rather than derive a wrong `prev_cover`
when `latest_summary()` returns `None` while the session's own hydration
read was truncated (byte budget) before ever finding a summary.

Real `CompactionController` + real `EventLog`/`CompactionConfig`
throughout — mirrors `test_compaction_summary_preamble_1820.py`'s own
established harness (stub engine/render/history_from_disk callables,
never the controller itself).
"""
from __future__ import annotations

import asyncio

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    HistoryChunkToCompact,
)
from tests._support.events import collect_events

_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=0, effective_trigger=0,
)


class _SucceedingEngine(CompactionEngine):
    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through=None,
    ) -> ChatSummary:
        return ChatSummary(topic_arc="STUB_ARC", covers_through_seq=0)


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    history: "list[ChatMessage]", *, latest_summary, history_load_truncated_unsafe,
    events: "EventLog | None" = None,
) -> "tuple[CompactionController, list, list]":
    disk_read_calls: "list[int]" = []

    def _history_from_disk(after_seq: int):
        disk_read_calls.append(after_seq)
        return [m for m in history if m.seq == 0 or m.seq > after_seq], False

    ctrl = CompactionController(
        event_log=events if events is not None else EventLog(),
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=_history_from_disk,
        latest_summary=latest_summary,
        compaction_engine_factory=_SucceedingEngine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers, *, covers_from_seq: ChatMessage(
            role="summary", content=rendered, seq=0,
        ),
        render_summary=lambda s: str(s),
        history_load_truncated_unsafe=history_load_truncated_unsafe,
    )
    return ctrl, history, disk_read_calls


def test_refuses_when_no_summary_and_history_load_was_truncated_unsafe():
    """Tier 2: accept — `latest_summary` returns `None` AND the injected
    flag says the hydration load was unsafe -> refuses BEFORE ever
    reading candidates from disk, outcome="history_load_unsafe", no
    summary appended.

    Strip: comment out the `if latest is None and self.
    _history_load_truncated_unsafe is not None and self.
    _history_load_truncated_unsafe(): ...` guard in force_compact_now —
    this goes RED (disk_read_calls non-empty, a summary IS appended,
    performed during review)."""
    hist = _history(7)
    events = EventLog()
    collected = collect_events(events)
    ctrl, hist, disk_read_calls = _make_controller(
        hist, latest_summary=lambda: None,
        history_load_truncated_unsafe=lambda: True,
        events=events,
    )

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.outcome == "history_load_unsafe"
    assert disk_read_calls == [], "must refuse BEFORE reading candidates from disk"
    assert not any(m.role == "summary" for m in hist), "must not fold anything"
    matching = [e for e in collected if e.type == "compaction_check"]
    assert any(e.data.get("outcome") == "history_load_unsafe" for e in matching), (
        "the refusal must be observable on the audit trail, not merely a "
        "silently-empty result"
    )


def test_proceeds_normally_when_a_summary_is_already_resident():
    """Tier 2: deny — even with the unsafe flag still True (it never
    literally clears, by design), a session that DOES have a resident
    summary (`latest_summary` returns one) is NOT refused — the ONLY
    unsafe case is `latest is None`, matching the exact defect this
    guards (an unknown prev_cover), not the flag's own historical
    truth."""
    hist = _history(7)
    summary = ChatMessage(role="summary", content="prior", seq=0, meta={"covers_through_seq": 0})
    ctrl, hist, disk_read_calls = _make_controller(
        hist, latest_summary=lambda: summary,
        history_load_truncated_unsafe=lambda: True,  # still True, by design
    )

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.outcome != "history_load_unsafe"
    assert disk_read_calls, "must proceed to read real candidates"


def test_proceeds_normally_when_the_flag_callable_is_none():
    """Tier 2: FP gate — a controller constructed WITHOUT the new
    parameter (every pre-#5939-PR-4 construction, and every OTHER test
    file's own `_make_controller` helper) behaves byte-identically —
    `None` means "never unsafe", never a refusal."""
    hist = _history(7)
    ctrl, hist, disk_read_calls = _make_controller(
        hist, latest_summary=lambda: None,
        history_load_truncated_unsafe=None,
    )

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.outcome != "history_load_unsafe"
    assert disk_read_calls


def test_proceeds_normally_when_flag_is_false():
    """Tier 2: deny — the flag callable exists but currently reports
    False (a session whose own hydration was never truncated) -> no
    refusal."""
    hist = _history(7)
    ctrl, hist, disk_read_calls = _make_controller(
        hist, latest_summary=lambda: None,
        history_load_truncated_unsafe=lambda: False,
    )

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.outcome != "history_load_unsafe"
    assert disk_read_calls
