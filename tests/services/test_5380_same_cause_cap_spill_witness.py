"""Tier 2: #5380 (filed as a remainder of #5378/#5367③, house rule 6) —
the same-cause-cap (non-byte) floor's own spill-resolves-overflow
witness, mirroring
``test_pr_n6_compaction_overflow_retry.py::test_5367_3_spill_before_raise_resolves_byte_limit_mid_split_floor``'s
shape but for the OTHER terminal floor #5378's shared
``_try_spill_first_mid_turn()`` helper also covers.

architect (#5378 co-vet): "『やらない』でなく『まだ答えていない』ので落とす
対象ではありません" — building the fixture (empty ``raw_middle``, refilled
from ``tail`` via retry_loop's own Phase 1, with a recurring non-byte cause)
was the part that didn't fit #5378's effort budget, not a decision to skip
the coverage.

Fixture shape: mirrors
``test_pr_n6_compaction_overflow_retry.py::test_retry_loop_same_cause_cap_raises_before_shrink_paths_exhausted``
almost exactly (same tail size, same ``_SameCauseOnCompactEngine`` shape,
same cap-triggering mechanics) — the ONE change is that ``tail[0]`` is a
real spillable ``role="tool"`` turn instead of generic filler, and
``compact()`` succeeds once THAT turn (wherever it lands in the offered
slice) has been replaced by the spilled content.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
from reyn.services.compaction.engine import (
    ComputedBudgets,
    ContextOverflowError,
    retry_loop,
)


def _make_cfg(**kwargs) -> CompactionConfig:
    defaults: dict = dict(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    defaults.update(kwargs)
    return CompactionConfig(**defaults)


_SPILLABLE_MARKER = "OVERSIZED_TOOL_RESULT"


class _OverflowingEngine:
    def __init__(self, fail_compact: bool = False) -> None:
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._fail_compact = fail_compact
        self._events = EventLog()
        self._T_comp_SP = 100


class _SameCauseOnCompactSpillableEngine(_OverflowingEngine):
    """Mirrors ``test_pr_n6_compaction_overflow_retry.py``'s own
    ``_SameCauseOnCompactEngine`` (same-cause, non-byte, unconditional
    ``ContextOverflowError``) EXCEPT: once the spillable marker is gone
    from the offered slice (spill_fn replaced it), compact() succeeds
    instead of recurring the same cause — the compact()-side witness
    that spill_fn's replacement actually reached engine.compact(), not
    just retry_loop's own state (mirrors the byte-limit floor's own
    ``_SpillableByteLimitEngine``)."""

    def __init__(self) -> None:
        super().__init__(fail_compact=False)
        self.compact_calls = 0
        # #5395 BLOCKING (lead-coder): `compact_calls` alone increments
        # on ENTRY, including the FIRST call — which always raises,
        # since the marker is still present before spill_fn ever runs.
        # A ``>= 1`` assert on that counter is satisfied on EVERY green
        # run regardless of whether the replacement ever reached
        # compact() at all, so it never actually witnessed this class's
        # own claimed property. This counter increments ONLY in the
        # marker-absent branch — the one call shape that can only be
        # reached once spill_fn's replacement has actually landed in
        # the turns compact() was given.
        self.compact_calls_with_marker_gone = 0

    async def compact(self, input_chunk):
        self.compact_calls += 1
        turns = input_chunk.new_turns
        if any(t.get("content") == _SPILLABLE_MARKER for t in turns if isinstance(t, dict)):
            raise ContextOverflowError("compact also overflows, same cause")
        self.compact_calls_with_marker_gone += 1
        from reyn.services.compaction.engine import ChatSummary
        return ChatSummary(
            topic_arc="ok",
            covers_through_seq=max((t.get("seq", 0) for t in turns if isinstance(t, dict)), default=0),
        )


def _turns(texts: list[str]) -> list[dict]:
    return [{"role": "user", "content": t, "seq": i + 1} for i, t in enumerate(texts)]


def test_5380_spill_resolves_the_same_cause_cap_non_byte_floor() -> None:
    """Tier 2: #5380 — at the same-cause-cap (non-byte) floor, a
    spillable ``raw_middle[0]`` (refilled from ``tail`` via retry_loop's
    own Phase 1, mid-chain) is offered to the injected ``spill_fn``
    BEFORE raising; if the spill produces smaller content that
    compact() then accepts, retry_loop returns normally instead of
    raising ``UnrecoveredError``.

    Falsification (performed during review): removing the
    ``_try_spill_first_mid_turn()`` call at the same-cause-cap raise
    site (reverting to #5367②'s text-only fix there) makes this test
    raise ``UnrecoveredError`` instead of returning.
    """
    cfg = _make_cfg()
    engine = _SameCauseOnCompactSpillableEngine()
    # Same override the fixture this mirrors uses (test_pr_n6_compaction_
    # overflow_retry.py's own test_retry_loop_same_cause_cap_raises_
    # before_shrink_paths_exhausted) — a tiny tail_budget so tail stays
    # "shrinkable" across several halvings instead of ever reading as
    # already-at-minimum.
    engine.budgets = ComputedBudgets(
        main_pool=100_000, head_budget=10, body_budget=500,
        tail_budget=10, new_msg_budget=10,
        B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
        section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                      "session_user_facts": 50, "artifacts_referenced": 175},
    )
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    # Same tail size as the (fixture this mirrors) same-cause-cap test —
    # large enough that Phase 1 (tail -> raw_middle refill) fires
    # partway through the same chain of recovers this test is building.
    # tail[0] is the ONE spillable turn; the rest are harmless filler.
    tail = [
        {"role": "tool", "content": _SPILLABLE_MARKER, "seq": 1,
         "tool_call_id": "tc-1", "name": "big_tool"},
        *_turns(["x" * 400] * 7),
    ]
    head: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 99}
    spill_calls: list = []

    def _spill_fn(turn: dict) -> "dict | None":
        spill_calls.append(turn)
        if turn.get("role") != "tool" or turn.get("content") != _SPILLABLE_MARKER:
            return None
        return {**turn, "content": "REF: spilled to .reyn/memory/history-content/..."}

    # main_call fails (same cause) as long as the spillable marker is
    # still reachable in ``tail`` (the only one of its kwargs that could
    # carry it — main_call never receives raw_middle directly), succeeds
    # once it is not: on iteration 0, raw_middle is empty so main_call is
    # what gets called first, and it must overflow to ever trigger Phase
    # 1 (tail -> raw_middle refill) at all. Once the marker has moved
    # into raw_middle (refilled) and later been resolved (spilled +
    # compacted away for good), tail no longer carries it either — so
    # main_call succeeds on ITS next call, same as a real turn once
    # nothing oversized remains anywhere in the payload.
    async def _main_call(**kwargs):
        if any(
            t.get("content") == _SPILLABLE_MARKER
            for t in kwargs.get("tail", []) if isinstance(t, dict)
        ):
            raise ContextOverflowError("main_call also overflows, same cause")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    result = asyncio.run(retry_loop(
        SP="sp", head=head, summary=None, raw_middle=[],
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_main_call,
        spill_fn=_spill_fn,
        max_iterations=8,
    ))

    assert result is not None, "retry_loop must return normally, not raise"
    # #5386 / #5395 BLOCKING (lead-coder review): this class's own
    # docstring names itself "the compact()-side witness that spill_fn's
    # replacement actually reached engine.compact(), not just
    # retry_loop's own state" — but a plain `compact_calls >= 1` does
    # NOT witness that claim: `compact_calls` increments on ENTRY, and
    # the FIRST compact() call always happens (and always raises)
    # BEFORE spill_fn ever runs, since the marker is still present at
    # that point. `>= 1` is therefore satisfied on every green run
    # regardless of whether the replacement ever reached compact() at
    # all. `compact_calls_with_marker_gone` increments ONLY in the
    # marker-absent branch — the one call shape reachable only once
    # spill_fn's replacement has genuinely landed in the turns
    # compact() was given — so THIS is the counter that actually
    # witnesses the claim. `>=` (not `==`) — the exact count is the
    # retry ladder's own implementation detail, not this test's subject
    # (the engine-direct-call sibling test, ..._mid_split_floor,
    # asserts `== 2` because IT calls the engine directly and the count
    # IS its subject there).
    assert engine.compact_calls_with_marker_gone >= 1, (
        "this class's own docstring claims to be the compact()-side "
        "witness that spill_fn's replacement reached engine.compact() "
        "— nothing verified that the replacement (not just any call) "
        "reached it, until now"
    )
    # Exactly one spill_fn call for the spillable turn — unpacking to one
    # element raises ValueError if retry_loop offered it a second time
    # (meaning the SAME object was offered for spilling twice).
    (only_spill_call,) = spill_calls
    assert only_spill_call["content"] == _SPILLABLE_MARKER, (
        "spill_fn must be offered the ORIGINAL content, not an "
        "already-spilled one"
    )
