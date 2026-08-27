"""Tier 2: #5256 — a provider usage-window/plan quota exhaustion (429
``usage_limit_reached``) must never be diagnosed as "context overflow" (never
enters the shrink/compaction retry path) and must never end the session.

Real reported incident (reyn-self, 2026-08-24..27): a 429 carrying
``{"type": "usage_limit_reached", ...}`` was treated as a shrinkable
context-overflow cause because its message text happened to contain the
word "limit" (``is_context_overflow_error``'s own keyword fallback). Each
shrink attempt itself made a compaction LLM call that ALSO hit the same
exhausted quota, burning more of it; after
``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS`` (2) repeats, ``retry_loop`` raised
``UnrecoveredError``, which the chat driver recorded as
``router_context_overflow_unrecovered`` (a lie — nothing overflowed) and
then let propagate, ending the session (owner ruling: "quota 枯渇で
session を終わらせてはいけない").

End-to-end through a real Session + RouterLoop (``call_llm_tools`` patched
at ``reyn.runtime.router_loop.call_llm_tools`` with a real async callable
that raises — never a mock, per testing.md) — exercises the REAL production
except-clause chain (``RouterLoopDriver._run_with_shrink`` ->
``run_turn`` -> ``Session._handle_inbox_text``'s generic catch-all), not a
re-implementation of it.
"""
from __future__ import annotations

import asyncio

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _QuotaExhaustedError(Exception):
    """Real, scripted stand-in for litellm's ``RateLimitError`` shape for a
    usage-window/plan quota exhaustion — the exact fields observed in the
    real incident's own ``llm_request_error`` event (issue #5256): a
    ``status_code`` and a structured ``.body`` dict litellm exposes from
    the provider's own parsed error response."""

    def __init__(self) -> None:
        super().__init__("The usage limit has been reached")
        self.status_code = 429
        self.body = {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "plus",
            "resets_at": 1788132890,
            "resets_in_seconds": 12258,
        }


def _drain_outbox(session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def test_quota_exhaustion_never_enters_shrink_and_never_ends_the_session(
    monkeypatch,
) -> None:
    """Tier 2: the acceptance witness for #5256's own two named defects.

    ① misdiagnosis: no compaction/shrink attempt happens (the LLM call
       fires exactly once — a shrink retry would make a second call).
    ② termination: the exception never reaches ``run_turn``'s own
       ``except (ContextOverflowError, UnrecoveredError)`` (no
       ``router_context_overflow_unrecovered`` event — the lying record
       the issue names) and never propagates out of ``_handle_inbox_
       text`` at all (the session survives to handle a next turn)."""
    session = make_session(agent_name="quota_test")
    collected = collect_events(session._audit_events)

    call_count = 0

    async def _fake_call_llm_tools(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _QuotaExhaustedError()

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools,
    )

    async def _drive() -> None:
        # Must not raise — the generic catch-all keeps the session alive.
        await session._handle_inbox_text("hi", chain_id="chain-quota-1")
        await settle(session._audit_events)

    # architect review (#5292): a separate asyncio.run() per await runs each
    # on its OWN event loop — today's dispatch consumers happen to drain
    # before either loop closes, but that is not guaranteed, and a day it
    # doesn't would surface as an unrelated flake (a join() that never
    # returns), not a real assertion failure. One loop for both awaits.
    asyncio.run(_drive())

    # ① never shrinkable: exactly ONE LLM call — a shrink retry would
    # have made a second one (and the real incident's own compaction
    # call would have made a THIRD, itself failing the same way).
    assert call_count == 1, (
        f"expected exactly 1 LLM call (no shrink retry), got {call_count}"
    )

    # ② the lying record must never fire for this cause.
    kinds = [e.type for e in collected]
    assert "router_context_overflow_detected" not in kinds, (
        "a quota exhaustion must never be classified as context overflow"
    )
    assert "router_context_overflow_unrecovered" not in kinds, (
        "a quota exhaustion is not an overflow — this record would lie "
        "to whoever reads it later (#5256)"
    )

    # The generic catch-all's own existing instrument DID fire — proof the
    # exception genuinely reached that handler (not swallowed earlier by
    # some other branch) and was reported, not silently dropped.
    terminated = [e for e in collected if e.type == "router_loop_terminated_by_exception"]
    assert terminated, "the exception must reach the generic catch-all's own P6 instrument"
    assert terminated[0].data["error_type"] == "_QuotaExhaustedError"

    # ③ operator-visible, decision-enabling message — not "wait a moment"
    # (which understates a multi-hour usage window), and not silence.
    msgs = _drain_outbox(session)
    error_msgs = [m for m in msgs if m.kind == "error"]
    assert error_msgs, "the operator must see something — not a silent end"
    assert "[usage limit]" in error_msgs[0].text
    # The provider's own resets_in_seconds value surfaces (#5256: resets_at
    # is NOT used — see quota_reset_seconds's own docstring for why).
    assert "12258" in error_msgs[0].text


def test_a_genuine_context_overflow_still_shrinks_unaffected(monkeypatch) -> None:
    """Tier 2: non-vacuity — the #5256 fix must not disturb the EXISTING,
    correct behaviour for a real context-window overflow (a completely
    different exception shape, no ``.body`` at all): it must still enter
    the shrink path. Proves the new quota check is a narrow addition, not
    a change to ``is_context_overflow_error``'s own established
    classification."""
    from reyn.runtime.error_format import is_quota_exhausted_error
    from reyn.services.compaction.engine import is_context_overflow_error

    class _PlainOverflowError(Exception):
        pass

    exc = _PlainOverflowError("maximum context length exceeded")
    assert is_quota_exhausted_error(exc) is False
    assert is_context_overflow_error(exc) is True

    # #5329 (architect review): this assertion used to pin the OPPOSITE
    # value (True) — documenting a genuine misdiagnosis: the quota
    # exception's own message ("The usage limit has been reached") DOES
    # match is_context_overflow_error's own "limit" keyword fallback, so
    # any call site reaching this predicate WITHOUT its own quota guard
    # first (unlike #5256's outer _run_with_shrink gate, which always
    # checks quota before calling this) would still misdiagnose a quota
    # exhaustion as an overflow. #5329 closed that at the single shared
    # predicate itself (is_quota_exhausted_error checked FIRST, inside
    # is_context_overflow_error) rather than requiring every call site to
    # remember its own guard — this now asserts the FIXED value.
    assert is_context_overflow_error(_QuotaExhaustedError()) is False
