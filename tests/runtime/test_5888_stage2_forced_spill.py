"""Tier 2: #5888 stage 2 (architect's 3″ ruling) — an explicit ``/compact``
that selected ZERO fold candidates (head/tail protection held every
eligible entry back) still runs rung① spill over the RESIDENT wire before
giving up, reaching exactly the content that was previously unreachable by
any lever.

owner real-machine incident (the root #5888 traces to): a 2.8 MB ``exec``
tool result sat in the tail's own keep-whole group (#2289) — never a fold
candidate (head/tail protection holds it back BY DESIGN), never reached by
the reactive ladder (that only fires on a genuine overflow, and #5719's
shortfall rule stops the reactive ladder from folding a middle that
already fits — see ``test_5888_operator_selection.py``). ``/compact``
therefore reported "all protected" and could do nothing about it. Spill
reaches this WITHOUT loosening protection: it replaces a tool result's
BODY with a reference, never splits or removes a message (#2289's own
keep-whole invariant is untouched — spill runs orthogonal to it).

Reuses ``test_5888_operator_selection.py``'s own ``_make_controller``
harness (real ``CompactionController``/``ChatMessage``/``EventLog``/
``CompactionConfig``) and ``test_5712_...``'s own real, working spill_fn
contract shape (content+spillability-shaped wire dicts in, ``(index,
replacement)`` edits out — the class of fake this suite's own docstrings
already establish as acceptable: the engine and spill_fn are the two
collaborators standing in for the provider LLM/network boundary and the
real ``RouterHistoryBuffer.spill_turn_content`` durable write,
respectively).
"""
from __future__ import annotations

import asyncio
from typing import Callable

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import CompactionEngine, ComputedBudgets
from tests._support.events import collect_events

# Same shape as test_5888_operator_selection.py's own _STUB_BUDGETS: head
# fits 1 turn, tail fits 1 turn, main_M_room fits 3 turns of unprotected
# middle — a 4-turn history is head+tail+an-already-fitting-middle, the
# owner's own shape (zero fold candidates, everything eligible protected).
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=150, effective_trigger=150,
)


class _NeverCalledEngine(CompactionEngine):
    """Fails the test outright if ``compact()`` is ever invoked — every
    test in this file has zero fold candidates, so no LLM call belongs on
    this path at all; spill is the ONLY thing that may run."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk, *, covers_through):  # noqa: ANN001
        raise AssertionError(
            "compact() must never be called on a zero-candidate pass — "
            "spill is orthogonal to fold, not a way to reach one"
        )


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine, events: EventLog,
) -> CompactionController:
    return CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
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


def _wire(content: str, *, spillability: str = "first_choice", seq: int = 1) -> dict:
    return {"role": "tool", "content": content, "spillability": spillability, "seq": seq}


def _decompose_returning(
    head: "list[dict]", raw_middle: "list[dict]", tail: "list[dict]",
) -> "Callable[[], tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]]":
    """A real ``decompose_history_for_retry()`` contract stand-in — the
    SAME shape ``force_compact_now`` reads (head, raw_middle, tail,
    summary, seq_by_id), ``seq_by_id`` id-keyed off the very wire dicts
    handed back (matching the real function's own guarantee, see its
    docstring), so a fake spill_fn threading ``seq_by_id`` through
    ``_mid_seq_of``-shaped lookups behaves identically to production."""
    seq_by_id = {id(w): w["seq"] for face in (head, raw_middle, tail) for w in face}

    def _decompose() -> "tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]":
        return head, raw_middle, tail, None, seq_by_id

    return _decompose


def _spill_once_per_call(
    spilled_faces: "list[list[dict]]",
) -> "Callable[..., list[tuple[int, dict]]]":
    """A real, working spill_fn contract (test_5712's own shape): spills
    the FIRST non-``never`` candidate offered, records which face it was
    called with (by identity — the exact list object ``force_compact_now``
    handed it) so a test can assert on call order/count without pinning
    an internal iteration detail."""

    def _spill_fn(
        offered: "list[dict]", *, seq_by_id: "dict[int, int] | None" = None,
    ) -> "list[tuple[int, dict]]":
        spilled_faces.append(offered)
        for idx, item in enumerate(offered):
            if item.get("spillability") == "never":
                continue
            content = item.get("content", "")
            if content.startswith("[spilled]"):
                continue
            return [(idx, {**item, "content": "[spilled]"})]
        return []

    return _spill_fn


def test_forced_compact_spills_a_protected_tail_group_when_fold_selected_nothing() -> None:
    """Tier 2: #5888 3″ accept ① — fold candidates 0 (head+tail protect
    everything eligible, the owner's own shape), tail has a large
    spillable tool result on the RESIDENT wire → ``/compact`` spills it
    and reports both the count and the bytes freed.

    strip witness (executed, not reasoned): reverting the ``if not
    candidates:`` branch to its pre-#5888-stage-2 form (unconditional
    early return, no spill attempt) makes ``spilled_count``/
    ``spilled_bytes_freed`` both 0 here — verified directly during this
    fix, restored after."""
    history = _history(4)
    events = EventLog()
    ctrl = _make_controller(history=history, engine=_NeverCalledEngine(), events=events)
    big = "y" * 5000
    decompose = _decompose_returning([], [], [_wire(big, seq=99)])
    spilled_faces: "list[list[dict]]" = []

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=_spill_once_per_call(spilled_faces),
            spill_capability_present=True,
            decompose_for_retry=decompose,
        ),
    )

    assert result.candidate_count == 0, "test setup sanity: this pass must select no fold candidate"
    assert result.spilled_count == 1, f"expected exactly one spilled result; got {result.spilled_count}"
    assert result.spilled_bytes_freed == len(big) - len("[spilled]"), (
        f"expected the exact char delta between the original body and its "
        f"spill placeholder; got {result.spilled_bytes_freed}"
    )
    assert result.spill_capability_present is True


def test_spill_tries_every_face_not_just_the_first_that_progresses() -> None:
    """Tier 2: #5888 3″ — unlike the REACTIVE ladder's own
    ``_attempt_reactive_spill`` (stops at the first face that progresses,
    since the reactive question is only "has enough happened to retry the
    call"), an operator's explicit ``/compact`` tries EVERY face: head
    progresses AND tail still gets its own spill attempt in the same
    pass — maximum effect for one explicit ask, matching #5888 ruling 2's
    own "operator mode widens what may fold, never what is protected"
    precedent for the shortfall/operator split.

    strip witness: an implementation that ``return``s after the first
    face with edits (the reactive shape) reports ``spilled_count == 1``
    here instead of 2 — verified directly, restored after."""
    events = EventLog()
    ctrl = _make_controller(history=_history(4), engine=_NeverCalledEngine(), events=events)
    head_face = [_wire("head body", seq=1)]
    raw_middle_face: "list[dict]" = []
    tail_face = [_wire("tail body", seq=99)]
    decompose = _decompose_returning(head_face, raw_middle_face, tail_face)
    spilled_faces: "list[list[dict]]" = []

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=_spill_once_per_call(spilled_faces),
            spill_capability_present=True,
            decompose_for_retry=decompose,
        ),
    )

    assert result.spilled_count == 2, (
        f"both the head and the tail candidate must be spilled in one pass; got {result.spilled_count}"
    )
    # Identity checks, not a count: each of the three faces
    # `decompose_history_for_retry()` returns — including the EMPTY
    # `raw_middle` — must be the exact object `force_compact_now` offers
    # to `spill_fn`, proving every face is tried rather than iteration
    # stopping at the first one that progresses.
    assert any(face is head_face for face in spilled_faces), (
        "spill_fn was never offered the head face"
    )
    assert any(face is raw_middle_face for face in spilled_faces), (
        "spill_fn was never offered the (empty) raw_middle face"
    )
    assert any(face is tail_face for face in spilled_faces), (
        "spill_fn was never offered the tail face"
    )


def test_spill_is_skipped_when_capability_is_absent() -> None:
    """Tier 2: #5888 3″ accept ② — ``spill_capability_present=False``
    (#5717's own fact: the attached driver has no spill mechanism at all)
    means the empty-candidates pass must not even ATTEMPT spill — proven
    here by handing it a ``decompose_for_retry`` that fails the test if
    it is ever called, mirroring ``_NeverCalledEngine``'s own "must not
    be reached" pattern."""
    def _must_not_be_called() -> "tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]":
        raise AssertionError(
            "decompose_for_retry must never be called when spill_capability_present is False"
        )

    events = EventLog()
    ctrl = _make_controller(history=_history(4), engine=_NeverCalledEngine(), events=events)

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=lambda _candidates, **_kw: [],
            spill_capability_present=False,
            decompose_for_retry=_must_not_be_called,
        ),
    )

    assert result.spilled_count == 0
    assert result.spilled_bytes_freed == 0
    assert result.spill_capability_present is False


def test_spill_is_skipped_when_decompose_is_not_wired() -> None:
    """Tier 2: #5888 3″ non-regression — the DEFAULT (``decompose_for_
    retry=None``, every pre-#5888-stage-2 caller's own behaviour, and the
    reactive ladder's own fallback caller in ``router_loop_driver.py``,
    which never passes this parameter at all): a zero-candidate pass
    degrades to exactly the pre-stage-2 outcome — no spill attempted, no
    crash — even with ``spill_capability_present=True``."""
    events = EventLog()
    ctrl = _make_controller(history=_history(4), engine=_NeverCalledEngine(), events=events)

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=lambda _candidates, **_kw: [],
            spill_capability_present=True,
        ),
    )

    assert result.candidate_count == 0
    assert result.spilled_count == 0
    assert result.spilled_bytes_freed == 0


def test_decompose_is_never_called_when_fold_selected_real_candidates() -> None:
    """Tier 2: #5888 3″ — the O(history bytes) decompose (#5898's own
    class: re-serialises every resident turn) is paid ONLY on the one
    pass where fold found nothing; a normal fold (candidates > 0) must
    never touch it, so this stage adds no cost to the common case.
    Proven with a fold that GENUINELY has candidates (main_M_room=0, so
    any nonempty middle overflows the room) and a decompose stand-in that
    fails the test if invoked."""
    def _must_not_be_called() -> "tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]":
        raise AssertionError("decompose_for_retry must never run when candidates were selected")

    budgets = ComputedBudgets(
        main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
        new_msg_budget=10_000, B_M=80_000, main_M_room=0, effective_trigger=0,
    )

    class _SucceedingEngine(CompactionEngine):
        def __init__(self, events: EventLog) -> None:
            self._model = ""
            self._events = events
            self._budgets = budgets

        async def compact(self, input_chunk, *, covers_through):  # noqa: ANN001
            from reyn.services.compaction.engine import ChatSummary
            seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
            return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)

    events = EventLog()
    history = _history(4)
    ctrl = _make_controller(history=history, engine=_SucceedingEngine(events), events=events)

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=lambda _candidates, **_kw: [],
            selection="operator",
            decompose_for_retry=_must_not_be_called,
        ),
    )

    assert result.candidate_count > 0, "test setup sanity: this pass must genuinely select candidates"
    assert result.spilled_count == 0, "spilled_count only ever means something on a 0-candidate pass"


def test_trim_head_and_tail_now_emit_the_kept_whole_event() -> None:
    """Tier 2: #5888 3″ observation ① (architect ruling) — the controller's
    own ``trim_head``/``trim_tail`` calls now pass ``events=self._events``
    (measured before this fix: 0 emissions from this path ever, though
    the emitter itself — ``engine.py``'s ``_emit_over_budget_group`` —
    already existed and the reactive ladder's own callers already wired
    it). A tool-cycle group (assistant-with-tool_calls + its own
    tool-role result) alone over the head budget now emits
    ``tool_cycle_kept_whole_over_budget`` where it silently emitted
    nothing before.

    strip witness: dropping either ``events=self._events`` argument
    (reverting to the bare ``trim_head(messages, head_budget, model,
    use_chars4=use_chars4)`` call) makes ``collect_events`` empty here —
    verified directly, restored after."""
    big_tool_cycle = [
        ChatMessage(
            role="assistant", content="", seq=1,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", content="z" * 400, tool_call_id="c1", name="exec", seq=2),
    ]
    history = big_tool_cycle + _history(1)  # a lone small tail turn so the group is unambiguously HEAD
    for i, m in enumerate(history[2:], start=3):
        m.seq = i
    events = EventLog()
    collected = collect_events(events)
    ctrl = _make_controller(history=history, engine=_NeverCalledEngine(), events=events)

    result = asyncio.run(
        ctrl.force_compact_now(spill_fn=lambda _candidates, **_kw: [], selection="operator"),
    )

    kept_whole = [e for e in collected if e.type == "tool_cycle_kept_whole_over_budget"]
    assert kept_whole, (
        "the over-head-budget tool cycle must emit tool_cycle_kept_whole_over_budget "
        f"via the controller's own trim_head call; collected types: {[e.type for e in collected]}"
    )
    assert kept_whole[0].data.get("budget_kind") == "head"
    assert result.protected_head_turns >= 2, "the whole tool cycle (2 turns) must be kept, not split"
