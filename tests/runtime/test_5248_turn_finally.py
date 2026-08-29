"""Tier 2: #5248 preserves turn boundaries across router terminal paths.

B and D regression witnesses for the production nested-finally and external-
cancel checkpoint behavior. A/C remain covered by existing turn and hard-cancel
contracts.

#5450 population note (this file was #7 of the issue's 11-file structural
needle, ``_loop_driver.run_turn *=``): only ``test_external_cancel_reaches_
cleanup_without_journal_cut`` migrated to ``@pytest.mark.llm_stub(control=
"gated")`` — see that test's own docstring for why. ``test_router_failure_
reaches_boundary_operations`` (``fail_run_turn``) stays on the private
replacement FOR NOW; see ITS docstring — architect correction (#5450,
2026-08-29): the exemption test is not "is this property LLM-boundary-
related" but "is there actually a MEANS to produce the raise through the
real boundary" (the same bar #5462's swallow-mode non-addition met, via a
measured litellm/httpx report). #5474 (open at time of writing) generalizes
LLMStub's raise mode past compaction-only — try that once it lands; only a
MEASURED inability to reproduce this exact shape through it (not "the
property seems unrelated to the LLM") justifies calling this permanent.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


@pytest.mark.asyncio
async def test_router_failure_reaches_boundary_operations() -> None:
    """Tier 2: a normal router exception still reaches hook, reload, and cut.

    Not YET migrated to LLMStub (#5450 population note, module docstring):
    at #5461, the raise mode was narrowly compaction-specific
    (``raise_for="compaction"``, discriminated by the FIXED
    ``COMPACTION_SYSTEM_PROMPT`` system message) — no discriminator existed
    for "the ordinary chat completion path, but make it raise a plain
    RuntimeError". #5474 generalizes ``raise_for`` past compaction-only;
    once it lands this should be tried for real. Production evidence this
    is likely reachable: ``router_loop_terminated_by_exception`` fires 83
    times in reyn-self (79 RateLimitError / 4 InternalServerError) — a real
    provider exception genuinely does propagate out of ``run_turn`` in
    production, so the same raise-based mechanism very plausibly reaches
    this shape too. Architect correction (#5450, 2026-08-29): "the LLM
    boundary is irrelevant to this property" is NOT itself an exemption —
    only a MEASURED inability to reproduce this exact raise through the
    real boundary (the same bar #5462's swallow-mode non-addition met)
    would make the private replacement here permanent rather than
    provisional."""
    session = make_session(agent_name="turn-finally-failure")
    events = collect_events(session._audit_events)
    calls: list[str] = []

    async def fail_run_turn(text: str, chain_id: str) -> None:
        raise RuntimeError("router failure")

    async def record_hook(*args: object, **kwargs: object) -> None:
        calls.append("hook")

    async def record_reload() -> None:
        calls.append("reload")

    async def record_cut(*, anchor: str, full_message: str) -> None:
        calls.append("cut")

    session._loop_driver.run_turn = fail_run_turn  # type: ignore[method-assign]
    session._hook_dispatcher.dispatch = record_hook  # type: ignore[method-assign]
    session._hot_reloader.apply_pending = record_reload  # type: ignore[method-assign]
    session._journal.cut_generation = record_cut  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="router failure"):
        await session._run_router_loop("hello", "failure-chain")
    await settle(session._audit_events)

    assert calls == ["hook", "reload", "cut"]
    assert not [event for event in events if event.type == "turn_completed"]


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_external_cancel_reaches_cleanup_without_journal_cut(_llm_stub) -> None:
    """Tier 2: external cancellation is not a user-facing checkpoint.

    #5450: turn_started does not fire here (this test calls production's
    ``_run_router_loop`` directly, bypassing ``run_one_iteration`` entirely
    — the level ``turn_started`` is emitted at). That does not mean witness
    ② is absent, only satisfied a different way: this test itself calls
    production's ``_run_router_loop``, with no substituted driver in the
    call chain to hide behind — the call itself is the witness that a real
    path ran."""
    session = make_session(agent_name="turn-finally-external")
    calls: list[str] = []

    async def record_hook(*args: object, **kwargs: object) -> None:
        calls.append("hook")

    async def record_reload() -> None:
        calls.append("reload")

    async def record_cut(*, anchor: str, full_message: str) -> None:
        calls.append("cut")

    session._hook_dispatcher.dispatch = record_hook  # type: ignore[method-assign]
    session._hot_reloader.apply_pending = record_reload  # type: ignore[method-assign]
    session._journal.cut_generation = record_cut  # type: ignore[method-assign]

    task = asyncio.create_task(session._run_router_loop("hello", "external-chain"))
    await _llm_stub.call_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["hook", "reload"]
