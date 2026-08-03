"""Tier 2: force-close ``covers_through_seq`` must not exceed what the wrap-up
call actually fed the LLM (#3599), and ``dropped_seq_ranges`` must also name
any ``head`` seqs the watermark claims covering without ever feeding them
(#3658).

``_force_close_wrap_up`` has a 3-tier bounded fallback that shrinks its input
on overflow: ``[summary + raw_middle + tail]`` -> ``[summary + tail]`` ->
``[summary]``. Before this fix, the caller (``_force_close_handoff``) recorded
``covers_through_seq = next_seq() - 1`` UNCONDITIONALLY — "everything up to
right now is covered" — regardless of which tier actually won, so a fallback
that dropped ``raw_middle`` (or ``raw_middle`` + ``tail``) from the
summarisation input still had its watermark claim the FULL range as covered.

#3658: ``head`` (the earliest token-budget slice) is NEVER part of any
candidate, in every tier — but it was entirely absent from
``dropped_seq_ranges`` too, so a head seq the watermark ended up claiming as
covered (because ``covers`` advanced past it) was reported nowhere as lost.
The fix adds the ``head`` seqs in ``(prev_cover, covers]`` — the only ones
this call's OWN watermark claims responsibility for without feeding — to
``dropped_seq_ranges``. At the summary-only tier ``covers == prev_cover``,
so this interval is empty BY CONSTRUCTION (an intersection, not a tier
branch) — see ``test_tier3_fallback_pins_covers_to_prior_watermark_not_next_seq``.

Driven through the real ``_force_close_handoff`` + real ``_force_close_wrap_up``
against a hand-written fake RouterLoop (a collaborator double, not a Mock)
whose ``_force_close_call`` overflows a controlled number of times before
succeeding — forcing a specific fallback tier to win. All turns are role="user"
so every one gets a real monotonic ``.seq`` via ``Session._append_history``
(the real assignment path — bypassing the assign step, e.g. via a bare
``session.history.append``, would leave every turn at ``seq=0`` and make the
by-value assertions below vacuous).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.chat_message import ChatMessage
from reyn.services.compaction.engine import ContextOverflowError
from tests._support.session import make_session as _make_session

# Content sized (with use_chars4_estimate=True, 1 token per 4 chars) so 8 turns
# of this content elide into head=[t0] / raw_middle=[t1..t6] / tail=[t7] at
# t_max=2800 (the same construction test_retry_loop_chat_wiring_1125.py's
# _make_session docstring measures: head_budget~74, tail_budget~112,
# effective_trigger~489; 320 chars -> 80 tokens/turn; 8*80=640 > 489 -> elide).
_TURN_80TOK = "X" * 320
_T_MAX = 2800


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push_user(session, text: str) -> None:
    """Append a REAL user turn through Session._append_history so it gets a
    genuine monotonic .seq (role="user" is the only role the seq-assignment
    gate fires for) — required for the by-value seq assertions below."""
    session._append_history(ChatMessage(role="user", content=text, ts=_now()))


class _FailFirstThenSucceed:
    """Collaborator double for RouterLoop: ``_force_close_call`` raises a
    context-overflow error on the first ``fail_first`` attempts, then
    succeeds — drives the bounded fallback to land on a specific tier."""

    def __init__(self, fail_first: int) -> None:
        self.attempts = 0
        self._fail_first = fail_first
        self.seen_inputs: list[list[dict]] = []

    async def _force_close_call(
        self, messages: list[dict], *, resolved_model: str
    ) -> LLMToolCallResult:
        self.attempts += 1
        self.seen_inputs.append(messages)
        if self.attempts <= self._fail_first:
            raise ContextOverflowError("wrap-up input too large")
        return LLMToolCallResult(
            content="CONSOL", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
        )


def _capture_events(session) -> list[Any]:
    seen: list[Any] = []
    session._chat_events.add_subscriber(lambda e: seen.append(e))
    return seen


def _push_8_turns(session) -> None:
    for _ in range(8):
        _push_user(session, _TURN_80TOK)


@pytest.mark.asyncio
async def test_top_tier_success_covers_matches_actually_fed_seqs(tmp_path) -> None:
    """Tier 2: when the FULL candidate (raw_middle + tail, no fallback) wins,
    covers_through_seq equals the max seq actually fed. #3658: ``head`` is
    still never fed, and here ``covers`` (== max of raw_middle+tail) advances
    past head's own seq, so head's span IS claimed-but-unfed and must be
    reported."""
    session = _make_session(tmp_path, t_max=_T_MAX)
    _push_8_turns(session)
    head, raw_middle, tail, _summary, seq_by_id = (
        session._history_buffer.decompose_history_for_retry()
    )
    assert head and raw_middle and tail, (
        "test premise: this construction must actually elide into a "
        "non-empty head/raw_middle/tail split for the scenario to exercise "
        "the fallback tiers below"
    )
    expected_covers = max(seq_by_id[id(t)] for t in raw_middle + tail)
    expected_head_span = [
        min(seq_by_id[id(t)] for t in head),
        max(seq_by_id[id(t)] for t in head),
    ]
    events = _capture_events(session)
    loop = _FailFirstThenSucceed(fail_first=0)  # top tier succeeds immediately

    await session._loop_driver._force_close_handoff(loop=loop, user_text="x")

    assert loop.attempts == 1
    (fired,) = [e for e in events if e.type == "router_force_close_handoff"]
    assert fired.data["covers_through_seq"] == expected_covers
    assert fired.data["dropped_seq_ranges"] == [expected_head_span], (
        "head is never fed to any candidate (#3658); covers advanced past "
        "head's seq here, so head's span is claimed-but-unfed and must "
        "appear in dropped_seq_ranges even though the top tier itself "
        "dropped nothing"
    )


@pytest.mark.asyncio
async def test_tier2_fallback_does_not_overclaim_and_reports_the_drop(
    tmp_path,
) -> None:
    """Tier 2: when the fallback shrinks to [summary + tail] (raw_middle
    dropped from the summarisation input), covers_through_seq must still
    equal the max seq ACTUALLY fed (tail's own seq — happens to equal the old
    formula's next_seq()-1 here, since tail holds the newest turn) — the
    requirement is asserted BY VALUE (not "old == new"), and the dropped
    raw_middle span must be reported so the loss is legible, not silently
    absorbed. #3658: covers also advances past head's own seq here, so
    head's span is claimed-but-unfed too, alongside raw_middle."""
    session = _make_session(tmp_path, t_max=_T_MAX)
    _push_8_turns(session)
    head, raw_middle, tail, _summary, seq_by_id = (
        session._history_buffer.decompose_history_for_retry()
    )
    assert raw_middle and tail, (
        "test premise: this construction must produce a non-empty "
        "raw_middle AND tail for the tier-2 fallback (raw_middle dropped, "
        "tail kept) to mean anything"
    )
    expected_covers = max(seq_by_id[id(t)] for t in tail)
    expected_dropped_span = [
        min(seq_by_id[id(t)] for t in raw_middle),
        max(seq_by_id[id(t)] for t in raw_middle),
    ]
    expected_head_span = [
        min(seq_by_id[id(t)] for t in head),
        max(seq_by_id[id(t)] for t in head),
    ]
    events = _capture_events(session)
    loop = _FailFirstThenSucceed(fail_first=1)  # top tier overflows once, tier2 fits

    await session._loop_driver._force_close_handoff(loop=loop, user_text="x")

    assert loop.attempts == 2
    (fired,) = [e for e in events if e.type == "router_force_close_handoff"]
    assert fired.data["covers_through_seq"] == expected_covers
    # #3658: order is not a declared contract — compare as a set of spans,
    # not a pinned sequence (see PR #3661 review).
    assert sorted(map(tuple, fired.data["dropped_seq_ranges"])) == sorted(
        map(tuple, [expected_dropped_span, expected_head_span])
    )


@pytest.mark.asyncio
async def test_tier3_fallback_pins_covers_to_prior_watermark_not_next_seq(
    tmp_path,
) -> None:
    """Tier 2: THE #3599 regression case — when even [summary + tail]
    overflows and the fallback lands on [summary] ALONE — nothing new fed to
    the LLM at all — covers_through_seq must NOT advance past the prior
    summary's own watermark (0, no summary yet here). Before the fix this
    unconditionally recorded ``next_seq() - 1`` (== 8), claiming the entire
    conversation was summarised when NONE of it reached the wrap-up call —
    the exact defect #3599 names. Both raw_middle's and tail's seq span are
    reported as dropped.

    #3658: head's span is ABSENT from dropped_seq_ranges here — not because
    this tier special-cases head away, but because the (prev_cover, covers]
    interval that gates it is empty BY CONSTRUCTION at this tier (covers ==
    prev_cover == 0, nothing new got fed). ``head`` itself is asserted
    non-empty below so the absence is provably the intersection collapsing,
    not head having nothing to offer in the first place."""
    session = _make_session(tmp_path, t_max=_T_MAX)
    _push_8_turns(session)
    head, raw_middle, tail, _summary, seq_by_id = (
        session._history_buffer.decompose_history_for_retry()
    )
    assert raw_middle and tail, (
        "test premise: this construction must produce a non-empty "
        "raw_middle AND tail for the tier-3 fallback (both dropped) to "
        "mean anything"
    )
    assert head, (
        "test premise: head must be non-empty so that its absence from "
        "dropped_seq_ranges below is provably due to the (prev_cover, "
        "covers] interval being empty, not head being trivially empty"
    )
    next_seq_minus_1 = session._next_seq - 1
    assert next_seq_minus_1 == max(seq_by_id[id(t)] for t in raw_middle + tail), (
        "test premise: every pushed turn is role=user, so the global seq "
        "counter must have advanced in lockstep with the newest turn "
        "actually present in raw_middle/tail"
    )
    expected_dropped_span = [
        min(seq_by_id[id(t)] for t in raw_middle + tail),
        max(seq_by_id[id(t)] for t in raw_middle + tail),
    ]
    events = _capture_events(session)
    loop = _FailFirstThenSucceed(fail_first=2)  # top tier + tier2 both overflow

    await session._loop_driver._force_close_handoff(loop=loop, user_text="x")

    assert loop.attempts == 3
    (fired,) = [e for e in events if e.type == "router_force_close_handoff"]
    assert fired.data["covers_through_seq"] == 0
    assert fired.data["covers_through_seq"] != next_seq_minus_1, (
        "the pre-fix defect: covers_through_seq recorded next_seq()-1 "
        "(claiming full coverage) even though the wrap-up input was shrunk "
        "to the persisted summary alone — nothing new was fed to the LLM"
    )
    # #3658: head's (prev_cover, covers] gate is 0 < seq <= 0 here — empty by
    # construction (an intersection), not a tier branch that special-cases
    # head away. Assert the interval's own emptiness, not just its effect.
    _prev_cover_this_call = 0
    assert not [
        t for t in head
        if _prev_cover_this_call < seq_by_id[id(t)] <= fired.data["covers_through_seq"]
    ]
    assert fired.data["dropped_seq_ranges"] == [expected_dropped_span]
    # The installed summary message itself must carry the same bounded value
    # (this is what downstream consumers like Session._uncompacted_tool_call_
    # records actually read back from history — not just the audit event).
    latest = session._latest_summary()
    assert latest is not None
    assert (latest.meta or {}).get("covers_through_seq") == 0
