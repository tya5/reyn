"""Tier 2: force-close ``covers_through_seq`` must not exceed what the wrap-up
call actually fed the LLM (#3599).

``_force_close_wrap_up`` has a 3-tier bounded fallback that shrinks its input
on overflow: ``[summary + raw_middle + tail]`` -> ``[summary + tail]`` ->
``[summary]``. Before this fix, the caller (``_force_close_handoff``) recorded
``covers_through_seq = next_seq() - 1`` UNCONDITIONALLY — "everything up to
right now is covered" — regardless of which tier actually won, so a fallback
that dropped ``raw_middle`` (or ``raw_middle`` + ``tail``) from the
summarisation input still had its watermark claim the FULL range as covered.

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
    covers_through_seq equals the max seq actually fed — byte-identical to the
    pre-fix value here (tail already reaches the newest seq), no dropped
    ranges. Establishes the non-regression baseline before the fallback cases
    below actually change behaviour."""
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
    events = _capture_events(session)
    loop = _FailFirstThenSucceed(fail_first=0)  # top tier succeeds immediately

    await session._loop_driver._force_close_handoff(loop=loop, user_text="x")

    assert loop.attempts == 1
    (fired,) = [e for e in events if e.type == "router_force_close_handoff"]
    assert fired.data["covers_through_seq"] == expected_covers
    assert fired.data["dropped_seq_ranges"] == []


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
    absorbed."""
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
    events = _capture_events(session)
    loop = _FailFirstThenSucceed(fail_first=1)  # top tier overflows once, tier2 fits

    await session._loop_driver._force_close_handoff(loop=loop, user_text="x")

    assert loop.attempts == 2
    (fired,) = [e for e in events if e.type == "router_force_close_handoff"]
    assert fired.data["covers_through_seq"] == expected_covers
    assert fired.data["dropped_seq_ranges"] == [expected_dropped_span]


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
    reported as dropped."""
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
    assert fired.data["dropped_seq_ranges"] == [expected_dropped_span]
    # The installed summary message itself must carry the same bounded value
    # (this is what downstream consumers like Session._uncompacted_tool_call_
    # records actually read back from history — not just the audit event).
    latest = session._latest_summary()
    assert latest is not None
    assert (latest.meta or {}).get("covers_through_seq") == 0
