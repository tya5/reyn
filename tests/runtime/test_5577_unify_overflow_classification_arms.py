"""Tier 2: #5577 — both `router_loop_driver.py` overflow-classification
arms now classify through `classify_llm_failure`, not
`is_context_overflow_error` alone.

Lead-coder's own trace (#5577): two call sites both catch an exception and
decide "is this overflow, enter the shrink ladder" — arm① at
``_run_with_shrink``'s outer except (line ~584, decides whether to enter
``retry_loop`` at all), arm② inside ``_router_main_call`` (line ~741,
decides whether a RETRY within an already-entered ladder wraps as
``ContextOverflowError``). Both previously called
``is_context_overflow_error`` directly — arm① additionally hand-excluded
ONLY quota (``is_quota_exhausted_error``) first; arm② excluded nothing at
all. Neither excluded a FATAL exception (a plain ``AttributeError``/
``TypeError``/``KeyError`` in reyn's own glue code) or a RETRYABLE one
(5xx/timeout/connection failure) whose ``str()`` happened to contain an
overflow keyword ("context"/"token"/"length"/"limit"/"too long"/"too
large") — both classes got misdiagnosed as overflow and entered the
shrink ladder, burning real LLM calls chasing a cause no amount of
shrinking can fix (#3783's own owner ruling — the defect class #5543
created ``classify_llm_failure`` to close, left open on these two arms).

Falsified before writing (issue's own 3-point ask):
① Is ``is_quota_exhausted_error``'s manual pre-check in arm① safe to
   remove? Read both implementations directly:
   ``classify_llm_failure``'s RETRYABLE branch (engine.py) calls
   ``is_quota_exhausted_error(exc)`` — the SAME function, first thing
   checked after FATAL — so a quota exception classifies RETRYABLE
   (never OVERFLOW) identically either way. No counter-example exists.
② What does unifying arm② change, and is it correct? Previously-
   misclassified FATAL/RETRYABLE-but-keyword-matching exceptions no
   longer enter the shrink ladder — the exact direction #3783's owner
   ruling (quoted in the source) already established as correct.
③ `git grep is_context_overflow_error -- src/` — exactly these 2 call
   sites remain (`router_loop_driver.py:~537,~741`); the "3 inline
   copies" `engine.py:879`'s own comment names are historical (#3783
   stage 1 already unified them before this issue existed).

Real ``Session``/``RouterLoop`` throughout (``call_llm_tools`` patched at
``reyn.runtime.router_loop.call_llm_tools`` with a real async callable
that raises — never a mock, per testing.md) — same idiom
``tests/runtime/test_5256_quota_not_context_overflow.py`` already
establishes for this exact call-site family.
"""
from __future__ import annotations

import asyncio

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


def test_arm1_fatal_exception_does_not_enter_shrink_ladder(monkeypatch) -> None:
    """Tier 2: #5577 accept — arm① (``_run_with_shrink``'s outer except).

    A FATAL-shaped exception (``AttributeError`` whose message contains
    "token" — the exact shape #5568's own incident produced) on the VERY
    FIRST LLM call must not enter the shrink ladder: exactly one LLM call,
    no ``router_context_overflow_detected`` event, the session survives.
    """
    session = make_session(agent_name="arm1_fatal_test")
    collected = collect_events(session)

    call_count = 0

    async def _fake_call_llm_tools(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise AttributeError("'NoneType' object has no attribute 'token'")

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools,
    )

    async def _drive() -> None:
        await session._handle_inbox_text("hi", chain_id="chain-arm1-fatal")
        await settle(session)

    asyncio.run(_drive())

    assert call_count == 1, (
        f"expected exactly 1 LLM call (no shrink retry entered), got {call_count}"
    )
    kinds = [e.type for e in collected]
    assert "router_context_overflow_detected" not in kinds, (
        "a FATAL exception (AttributeError) must never be classified as "
        "context overflow, even though its message contains 'token'"
    )
    terminated = [e for e in collected if e.type == "router_loop_terminated_by_exception"]
    assert terminated, "the exception must reach the generic catch-all's own P6 instrument"
    assert terminated[0].data["error_type"] == "AttributeError"


def test_arm1_genuine_overflow_still_shrinks(monkeypatch) -> None:
    """Tier 2: #5577 deny — arm① still classifies a REAL overflow as
    overflow and enters the shrink ladder (rules out an "always deny"
    implementation that would trivially pass the FATAL test above by
    never entering the ladder for anything)."""
    session = make_session(agent_name="arm1_overflow_test")
    collected = collect_events(session)

    async def _fake_call_llm_tools(*args, **kwargs):
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools,
    )

    async def _drive() -> None:
        await session._handle_inbox_text("hi", chain_id="chain-arm1-overflow")
        await settle(session)

    asyncio.run(_drive())

    kinds = [e.type for e in collected]
    assert "router_context_overflow_detected" in kinds, (
        "a genuine context-overflow-shaped exception must still enter the "
        "shrink ladder — arm① must not have become 'never classify as "
        "overflow'"
    )


def test_arm2_fatal_exception_on_a_retry_stops_the_ladder(monkeypatch) -> None:
    """Tier 2: #5577 accept — arm② (inside ``_router_main_call``, the
    retry_loop-injected callable).

    First LLM call raises a genuine overflow (arm① classifies OVERFLOW,
    enters retry_loop). The SECOND call — made by retry_loop's own
    ``main_call`` invocation, arm②'s own except block — raises a FATAL
    exception (AttributeError). Must NOT be wrapped as
    ``ContextOverflowError`` and must NOT trigger a third call (proving
    the ladder stopped rather than continuing to shrink a cause no amount
    of shrinking can fix)."""
    session = make_session(agent_name="arm2_fatal_test")
    collected = collect_events(session)

    call_count = 0

    async def _fake_call_llm_tools(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("maximum context length exceeded")
        raise AttributeError("'NoneType' object has no attribute 'token'")

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools,
    )

    async def _drive() -> None:
        await session._handle_inbox_text("hi", chain_id="chain-arm2-fatal")
        await settle(session)

    asyncio.run(_drive())

    assert call_count == 2, (
        f"expected exactly 2 LLM calls (1st overflow enters the ladder, "
        f"2nd FATAL stops it — never a 3rd shrink attempt), got {call_count}"
    )
    terminated = [e for e in collected if e.type == "router_loop_terminated_by_exception"]
    assert terminated, "the un-wrapped AttributeError must reach the generic catch-all"
    assert terminated[0].data["error_type"] == "AttributeError", (
        "arm② must propagate the FATAL exception UNWRAPPED — not as "
        "ContextOverflowError/UnrecoveredError, which would misreport the "
        "cause as an overflow that never happened"
    )
