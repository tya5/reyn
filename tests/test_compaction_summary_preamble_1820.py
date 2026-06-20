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
from dataclasses import dataclass, field

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    HistoryChunkToCompact,
)

_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=65_000, effective_trigger=65_000,
)


@dataclass
class _FakeMessage:
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)


class _SucceedingEngine(CompactionEngine):
    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        return ChatSummary(topic_arc="STUB_ARC", covers_through_seq=0)


def _make_controller(history: list[_FakeMessage]) -> tuple[CompactionController, list]:
    ctrl = CompactionController(
        event_log=EventLog(),
        config=CompactionConfig(use_chars4_estimate=True),
        history_access=lambda: list(history),
        latest_summary=lambda: None,
        compaction_engine=_SucceedingEngine(),
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, history


def _history(n: int) -> list[_FakeMessage]:
    return [
        _FakeMessage(role="user" if i % 2 == 1 else "assistant", text="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def test_summary_leads_with_reference_only_preamble():
    """Tier 2: the rendered summary leads with the reference-only preamble carrying
    the source-of-truth + discard-pending-on-reverse-signal directives."""
    ctrl, hist = _make_controller(_history(7))
    asyncio.run(ctrl.force_compact_now())
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
    asyncio.run(ctrl.force_compact_now())
    text = [m for m in hist if m.role == "summary"][-1].text
    assert "--- summary follows ---" in text, "delimiter between preamble and summary"
    assert "STUB_ARC" in text, "the original rendered summary content must survive the prepend"
