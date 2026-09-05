"""Tier 2: #5765 — a head-protected turn (``trim_head``'s #5719 guard,
never actually folded) must not be silently hidden from the wire
projection just because its ``seq`` sits below the latest summary's
``covers_through_seq``.

Real ``CompactionController`` + real ``ChatMessage``/``EventLog``/
``CompactionConfig`` + real ``RouterHistoryBuffer`` throughout — only the
engine (the LLM-call boundary every other compaction test here also
stubs) is a stand-in, and it captures exactly what it was asked to fold so
the test can assert on it directly. Adapted from the drove repro used to
CONFIRM #5765 against real code (posted verbatim as an issue comment on
#5765) into a permanent regression witness.
"""
from __future__ import annotations

import asyncio

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    HistoryChunkToCompact,
)

# head fits exactly 1 turn (50 tokens via chars4, "x"*200), tail fits
# exactly 1 turn, main_M_room=0 so every unprotected middle turn is
# needed to close the (nonzero, #5719) shortfall — same stub shape
# ``test_compaction_controller_invariants.py`` already uses.
_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000,
    tail_budget=50, new_msg_budget=10_000,
    B_M=80_000, main_M_room=0, effective_trigger=0,
)

_TURN_CHARS = 200  # 200 chars / 4 (chars4 estimate) == 50 tokens/turn.


def _turn_content(i: int) -> str:
    tag = f"turn-{i:03d}-"
    return tag + "x" * (_TURN_CHARS - len(tag))


class _CapturingEngine(CompactionEngine):
    """Real ``CompactionEngine`` subclass (never a mock) whose ``compact``
    records exactly which wire turns it was asked to fold, without ever
    calling an LLM."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _BUDGETS
        self.captured_seqs: "list[int]" = []

    async def compact(self, input_chunk: HistoryChunkToCompact, *, covers_through):
        seqs = [
            int(t.get("seq", 0)) for t in input_chunk.messages
            if isinstance(t, dict)
        ]
        self.captured_seqs = seqs
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


async def _null_spill_fn(pool, offered, attempt_len, **kw):
    return []


def _make_controller(history: list, engine: _CapturingEngine) -> CompactionController:
    return CompactionController(
        event_log=EventLog(),
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: next(
            (m for m in reversed(history) if m.role == "summary"), None,
        ),
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        # Production shape (session.py's own make_summary_message lambda,
        # #5765): covers_from_seq is a REQUIRED keyword-only argument.
        make_summary_message=lambda rendered, structured, covers, *, covers_from_seq: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={
                "structured": structured,
                "covers_through_seq": covers,
                "covers_from_seq": covers_from_seq,
            },
        ),
        render_summary=lambda s: str(s),
    )


def _make_buffer(history: list, controller: CompactionController) -> RouterHistoryBuffer:
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=controller,
        model_fn=lambda: "openai/gpt-4o",
        events=EventLog(),
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )


def test_head_protected_turn_survives_the_wire_projection_after_a_fold(tmp_path):
    """Tier 2: #5765 regression witness: seq=1 (never folded — protected by
    ``trim_head``) must still be present in ``build_history``'s own
    projection after a real compaction fold, and the persisted summary
    must record a ``covers_from_seq`` that excludes it from the elided
    range."""
    history = [
        ChatMessage(role="user" if i % 2 else "assistant", content=_turn_content(i), seq=i)
        for i in range(1, 51)  # seq 1..50
    ]
    engine = _CapturingEngine()
    ctrl = _make_controller(history, engine)

    result = asyncio.run(ctrl.force_compact_now(
        spill_fn=_null_spill_fn, spill_capability_present=False,
    ))
    assert not result.failed, f"expected a successful synchronous fold, got {result!r}"

    summary = next((m for m in reversed(history) if m.role == "summary"), None)
    assert summary is not None, "expected a summary to have been persisted"
    covers_through = summary.meta["covers_through_seq"]
    covers_from = summary.meta["covers_from_seq"]

    # The engine was never offered seq=1 — trim_head protected it.
    assert 1 not in engine.captured_seqs, (
        f"seq=1 should be head-protected and never offered to the "
        f"summarizer; got {engine.captured_seqs!r}"
    )
    # covers_from_seq must exclude the head-protected turn from the range.
    assert covers_from is not None and covers_from > 1, (
        f"covers_from_seq={covers_from!r} must exclude the head-protected "
        f"seq=1 turn from the folded range"
    )
    assert covers_through >= max(engine.captured_seqs)

    buf = _make_buffer(history, ctrl)
    wire = buf.build_history()
    wire_turn_contents = {
        m["content"] for m in wire
        if isinstance(m.get("content"), str) and m["content"].startswith("turn-")
    }

    assert _turn_content(1) in wire_turn_contents, (
        "the #5765 defect: a head-protected-but-never-folded turn was "
        "wrongly hidden from the wire just because its seq sat below "
        "covers_through_seq — must be fixed by the covers_from_seq range"
    )
    assert _turn_content(50) in wire_turn_contents, (
        "the tail-protected turn must also survive (never at risk, "
        "sanity check on the fixture itself)"
    )
    for seq in range(2, 50):
        assert _turn_content(seq) not in wire_turn_contents, (
            f"turn-{seq:03d} was actually folded into the summary and must "
            f"stay out of the projection — a real, not silent, compaction"
        )


def test_decompose_history_for_retry_and_build_history_agree_on_survivors(tmp_path):
    """Tier 2: #5765 acceptance criterion 4 (lead-coder — architect's own claim
    here was grep-only, "確かめたのは grep の行だけ"): a DRIVEN test, not a
    re-read, proving ``decompose_history_for_retry`` (the reactive
    overflow ladder's own candidate builder) and ``build_history`` (the
    wire projection) return the SAME surviving raw-turn set for the same
    post-fold history — both must route through the identical
    ``_apply_watermark_filter``/``is_seq_still_active`` range check."""
    history = [
        ChatMessage(role="user" if i % 2 else "assistant", content=_turn_content(i), seq=i)
        for i in range(1, 51)
    ]
    engine = _CapturingEngine()
    ctrl = _make_controller(history, engine)
    result = asyncio.run(ctrl.force_compact_now(
        spill_fn=_null_spill_fn, spill_capability_present=False,
    ))
    assert not result.failed

    buf = _make_buffer(history, ctrl)

    wire = buf.build_history()
    build_history_survivors = {
        m["content"] for m in wire
        if isinstance(m.get("content"), str) and m["content"].startswith("turn-")
    }

    head, raw_middle, tail, _summary_dict, _seq_by_id = buf.decompose_history_for_retry()
    decompose_survivors = {
        w["content"] for w in (head + raw_middle + tail)
        if isinstance(w.get("content"), str) and w["content"].startswith("turn-")
    }

    assert build_history_survivors == decompose_survivors, (
        f"build_history and decompose_history_for_retry disagree on which "
        f"raw turns survive the same post-fold history — "
        f"build_history only: {build_history_survivors - decompose_survivors!r}, "
        f"decompose only: {decompose_survivors - build_history_survivors!r}"
    )
    # Non-trivial: the head-protected turn must be ON BOTH sides, not just
    # absent from both by coincidence (e.g. a bug that drops everything).
    assert _turn_content(1) in build_history_survivors
    assert _turn_content(1) in decompose_survivors


def test_legacy_summary_missing_covers_from_seq_hides_nothing(tmp_path):
    """Tier 2: #5765 (c) — the explicit, driven-not-guessed SAFE-SIDE decision for
    a summary persisted BEFORE this fix (no ``covers_from_seq`` in its
    meta at all): the projection must treat the missing field as "protect
    everything, hide nothing for this summary's own range" rather than
    silently repeating the #5765 defect under the OLD ceiling-only
    reading. history.jsonl is append-only, so nothing is destroyed by
    this choice — the wire payload can only grow, never lose content."""
    history: list = [
        ChatMessage(
            role="summary", content="a legacy, pre-#5765 summary", seq=0,
            meta={"structured": {"topic_arc": "test"}, "covers_through_seq": 30},
            # NOTE: no "covers_from_seq" key at all — the legacy shape.
        ),
    ]
    for i in range(1, 41):
        history.append(ChatMessage(
            role="user" if i % 2 else "assistant", content=_turn_content(i), seq=i,
        ))

    buf = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )

    wire = buf.build_history()
    wire_turn_contents = {
        m["content"] for m in wire
        if isinstance(m.get("content"), str) and m["content"].startswith("turn-")
    }

    for i in range(1, 41):
        assert _turn_content(i) in wire_turn_contents, (
            f"turn-{i:03d}: a legacy summary missing covers_from_seq must "
            f"hide NOTHING (safe-side fallback) — got survivors "
            f"{sorted(wire_turn_contents)!r}"
        )
