"""Tier 2: #5721 (architect ruling) — the 3rd instance of #5712's own
named class: a reactive path is protected, an operator-driven one is
bare. ``CompactionController._run_compaction`` (the seam every
operator-driven `/compact`/`force_compact_now` call reaches) built the
compaction LLM's own ``section_token_caps`` hint from
``cfg.section_token_caps.*`` — the STATIC legacy defaults (sum 1500
tokens) — regardless of the model's real context window. ``engine.py``'s
own ``compute_budgets`` already computes a window-relative
``section_caps`` dict (from ``section_weights``, normalised to
``body_budget``) and NAMES the static values as its own fallback
("Fallback: use CompactionSectionCaps legacy values") — and the
REACTIVE path (``engine.py``'s own ``_stage_fold``, the retry_loop-
internal ``compact()`` call) already reads that primary value. e2e-coder
measured this asymmetry directly (issue #5721's own thread, real
execution — a real Session pushed past its own real ``effective_
trigger``, a spy on the real ``CompactionEngine.compact()`` capturing
its actual argument) before this fix existed.

★ Architect's own explicit acceptance shape (issuecomment on #5721):
"要約が大きくなることを gate にしない... 受入は『送った値が
`budgets.section_caps` と一致する』で書いてください" — asserting that
a produced summary is BIGGER than before would pin a third party's
property (the model's own willingness to follow a hint, CLAUDE.md's
six-questions ①). Every test below asserts VALUE EQUALITY against
``budgets.section_caps`` — the SENT hint, never the model's own
response size.

Real ``CompactionController`` throughout, mirroring
``test_5719_compact_folds_only_the_shortfall.py``'s own
``_SucceedingEngine``/``_make_controller`` idiom — only the engine's
own ``compact()`` (the LLM-call boundary every sibling test file here
also stubs) is a stand-in, capturing the real ``input_chunk`` it
receives rather than faking that boundary's own logic.
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
    CoversThrough,
    HistoryChunkToCompact,
)

# A non-trivial, WINDOW-RELATIVE section_caps dict standing in for what
# compute_budgets() would actually compute for a real large-context
# model — deliberately NOT the static legacy values (200/400/400/200/
# 300), so a test asserting on THIS value can only pass if the primary
# (not the fallback) path was actually taken.
_WINDOW_RELATIVE_SECTION_CAPS = {
    "topic_arc": 2168, "decisions": 17349, "pending": 10843,
    "session_user_facts": 4337, "artifacts_referenced": 15180,
}
_STUB_BUDGETS_WITH_SECTION_CAPS = ComputedBudgets(
    main_pool=997_594, head_budget=99_759, body_budget=49_879,
    tail_budget=149_639, new_msg_budget=99_759, B_M=949_455,
    main_M_room=648_437, effective_trigger=648_437,
    section_caps=_WINDOW_RELATIVE_SECTION_CAPS,
)
_STUB_BUDGETS_NO_SECTION_CAPS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=150, effective_trigger=150,
    # section_caps left at its own default_factory=dict -> {} (empty,
    # falsy) -- the "engine never computed one" shape the fallback below
    # exists for.
)


class _CapturingEngine(CompactionEngine):
    """Stands in for the LLM-call boundary only — captures the real
    ``input_chunk`` this controller built, same shape as
    ``test_5719``'s own ``_SucceedingEngine``."""

    def __init__(self, budgets: ComputedBudgets) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = budgets
        self.captured_section_token_caps: "dict | None" = None

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        self.captured_section_token_caps = dict(input_chunk.section_token_caps)
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine,
) -> CompactionController:
    return CompactionController(
        event_log=engine._events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )


def test_compact_sends_the_window_relative_section_caps_when_the_engine_has_one() -> None:
    """Tier 2: #5721's own accept criterion, verbatim — the value SENT to
    compact() equals budgets.section_caps, never the static legacy
    dict, and never a size assertion on anything the model produced."""
    history = _history(10)
    engine = _CapturingEngine(_STUB_BUDGETS_WITH_SECTION_CAPS)
    ctrl = _make_controller(history=history, engine=engine)

    async def _drive() -> None:
        await ctrl._run_compaction(
            list(history), previous_summary=None,
            spill_fn=lambda _c: [],
        )

    asyncio.run(_drive())

    assert engine.captured_section_token_caps == _WINDOW_RELATIVE_SECTION_CAPS, (
        f"#5721 REGRESSION: /compact must send the SAME window-relative "
        f"section_caps the reactive path (engine.py's own _stage_fold) "
        f"already uses — got {engine.captured_section_token_caps!r}"
    )


def test_compact_falls_back_to_the_static_legacy_caps_when_the_engine_has_none() -> None:
    """Tier 2: deny/fallback side — mirrors engine.py's own _stage_fold
    fallback shape exactly (`self._bg.section_caps if self._bg.
    section_caps else {legacy}`). When budgets.section_caps is empty
    (the "engine never computed one" case), /compact must still fall
    back to the static legacy dict, never send an empty hint."""
    history = _history(10)
    engine = _CapturingEngine(_STUB_BUDGETS_NO_SECTION_CAPS)
    ctrl = _make_controller(history=history, engine=engine)
    legacy = CompactionConfig(use_chars4_estimate=True).section_token_caps

    async def _drive() -> None:
        await ctrl._run_compaction(
            list(history), previous_summary=None,
            spill_fn=lambda _c: [],
        )

    asyncio.run(_drive())

    assert engine.captured_section_token_caps == {
        "topic_arc": legacy.topic_arc,
        "decisions": legacy.decisions,
        "pending": legacy.pending,
        "session_user_facts": legacy.session_user_facts,
        "artifacts_referenced": legacy.artifacts_referenced,
    }
