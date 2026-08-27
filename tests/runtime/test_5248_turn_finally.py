"""Tier 2: #5248 preserves turn boundaries across router terminal paths.

B and D regression witnesses for the production nested-finally and external-
cancel checkpoint behavior. A/C remain covered by existing turn and hard-cancel
contracts.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


@pytest.mark.asyncio
async def test_router_failure_reaches_boundary_operations() -> None:
    """Tier 2: a normal router exception still reaches hook, reload, and cut."""
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
async def test_external_cancel_reaches_cleanup_without_journal_cut() -> None:
    """Tier 2: external cancellation is not a user-facing checkpoint."""
    session = make_session(agent_name="turn-finally-external")
    calls: list[str] = []

    async def wait_for_cancel(text: str, chain_id: str) -> None:
        await asyncio.Event().wait()

    async def record_hook(*args: object, **kwargs: object) -> None:
        calls.append("hook")

    async def record_reload() -> None:
        calls.append("reload")

    async def record_cut(*, anchor: str, full_message: str) -> None:
        calls.append("cut")

    session._loop_driver.run_turn = wait_for_cancel  # type: ignore[method-assign]
    session._hook_dispatcher.dispatch = record_hook  # type: ignore[method-assign]
    session._hot_reloader.apply_pending = record_reload  # type: ignore[method-assign]
    session._journal.cut_generation = record_cut  # type: ignore[method-assign]

    task = asyncio.create_task(session._run_router_loop("hello", "external-chain"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["hook", "reload"]
