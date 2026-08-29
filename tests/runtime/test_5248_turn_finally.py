"""Tier 2: #5248 preserves turn boundaries across router terminal paths.

B and D regression witnesses for the production nested-finally and external-
cancel checkpoint behavior. A/C remain covered by existing turn and hard-cancel
contracts.

#5450 population note (this file was #7 of the issue's 11-file structural
needle, ``_loop_driver.run_turn *=``): only ``test_external_cancel_reaches_
cleanup_without_journal_cut`` migrated to ``@pytest.mark.llm_stub(control=
"gated")`` — see that test's own docstring for why. ``test_router_failure_
reaches_boundary_operations`` (``fail_run_turn``) stays on the private
replacement; see ITS docstring for why #5382/#5461's raise mode (landed
after this file was originally scoped there) does not reach it either —
reported as a finding, not silently left unexplained.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


@pytest.mark.asyncio
async def test_router_failure_reaches_boundary_operations() -> None:
    """Tier 2: a normal router exception still reaches hook, reload, and cut.

    Not migrated to LLMStub (#5450 population note, module docstring): this
    test's subject is ``_run_router_loop``'s OWN exception-handling
    structure — does the finally chain still fire when ``run_turn`` raises
    ANY exception — independent of WHY it raised. #5382/#5461's LLMStub
    raise mode (``raise_for="compaction"``) is deliberately narrow: it
    discriminates a compaction call by its FIXED system-message constant
    (``COMPACTION_SYSTEM_PROMPT``) — there is no equivalent discriminator
    for "the ordinary chat completion path, but make it raise a plain
    RuntimeError", and adding one would be inventing a second, broader
    raise mode for a single test, not migrating onto an existing one. The
    private ``run_turn`` replacement here is the correct, permanent form —
    the general-exception-resilience property it tests has nothing to do
    with the LLM boundary specifically."""
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
