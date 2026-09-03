"""Tier 2: compaction summary carries the reference-only preamble (#1820 Part1).

#1820 Part1 prepends a STATIC reference-only preamble (Hermes SUMMARY_PREFIX analog)
to every rendered compaction summary so the model treats the summary as history —
not a fresh instruction — and does not re-execute `pending` work after a reverse
signal. This drives the real CompactionController with stub engine/render callables
(mirroring test_compaction_controller_invariants) and asserts the rendered summary
leads with the preamble while still carrying the original summary content.

Policy: real CompactionController + real EventLog/CompactionConfig; only the engine
and the render/append callables are stubs (the existing harness pattern). No mocks.
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

# #5719: main_M_room=0 — this file's own tests are about the rendered
# preamble, not the shortfall-selection algorithm itself (that has its own
# dedicated tests), so main_M_room always produces a shortfall for any
# nonempty middle, matching this file's pre-#5719 "everything between
# head/tail is a candidate" fixture shape.
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


def _make_controller(history: list[ChatMessage]) -> tuple[CompactionController, list]:
    ctrl = CompactionController(
        event_log=EventLog(),
        config=CompactionConfig(use_chars4_estimate=True),
        # #4472: history_from_disk(after_seq) -> (list, truncated=False),
        # same durable-store contract.
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=_SucceedingEngine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, history


def _history(n: int) -> list[ChatMessage]:
    # #2957 PR-A: real ChatMessage — see test_compaction_controller_invariants.py
    # for why the prior hand-rolled substitute (stray ``text`` field, no
    # ``content``) only produced "large turns" via the now-fixed
    # estimate_tokens_for_turn/ChatMessage type-mismatch bug.
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def test_summary_leads_with_reference_only_preamble():
    """Tier 2: the rendered summary leads with the reference-only preamble carrying
    the source-of-truth + discard-pending-on-reverse-signal directives."""
    ctrl, hist = _make_controller(_history(7))
    asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))
    summaries = [m for m in hist if m.role == "summary"]
    assert summaries, "force_compact_now must append a summary"
    text = summaries[-1].text
    assert text.startswith("[CONTEXT SUMMARY"), "summary must lead with the reference-only preamble"
    assert "single source of truth" in text, "must name the latest user message as source of truth"
    assert "CANCELLED" in text, "must direct discarding pending work on a reverse signal"


def test_preamble_is_prepended_not_replacing_summary():
    """Tier 2: (non-regression) the preamble is PREPENDED — the original rendered
    summary content still follows it (the summary is not replaced)."""
    ctrl, hist = _make_controller(_history(7))
    asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))
    text = [m for m in hist if m.role == "summary"][-1].text
    assert "--- summary follows ---" in text, "delimiter between preamble and summary"
    assert "STUB_ARC" in text, "the original rendered summary content must survive the prepend"
