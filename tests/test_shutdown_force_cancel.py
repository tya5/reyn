"""Tier 2: registry.shutdown hard-cancels a stuck run task so /quit can't hang.

A session.run task blocked mid-LLM-call (slow/hung provider) never reaches the
turn boundary to see the cooperative shutdown sentinel. Before the fix,
shutdown awaited it forever (the owner-reported /quit hang). Now shutdown gives a
short grace, then hard-cancels the straggler. This injects a never-completing run
task and asserts shutdown returns promptly (it would TimeoutError if it hung) and
the stuck task is cancelled.
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.runtime.registry as registry_mod
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


def _registry(tmp_path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    # session_factory is never invoked here — no session is spawned; we inject a
    # stuck run task directly to model "a turn blocked mid-LLM-call".
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: None,
        state_log=state_log,
    )


@pytest.mark.asyncio
async def test_shutdown_force_cancels_a_stuck_run_task(tmp_path) -> None:
    """Tier 2: shutdown returns (grace + hard-cancel) instead of hanging on a
    run task that never completes; the stuck task ends up cancelled."""
    reg = _registry(tmp_path)
    saved_grace = registry_mod._SHUTDOWN_GRACE_S
    registry_mod._SHUTDOWN_GRACE_S = 0.05  # short grace → fast, deterministic test
    stuck = asyncio.ensure_future(asyncio.Event().wait())  # never completes
    reg._tasks[("stuck", "main")] = stuck
    try:
        # wait_for raises TimeoutError if shutdown hangs (the pre-fix behaviour).
        await asyncio.wait_for(reg.shutdown(), timeout=2.0)
        assert stuck.cancelled()
    finally:
        registry_mod._SHUTDOWN_GRACE_S = saved_grace
        if not stuck.done():
            stuck.cancel()


@pytest.mark.asyncio
async def test_shutdown_returns_quickly_when_no_tasks(tmp_path) -> None:
    """Tier 2: the common path (nothing running) returns without waiting the
    grace — no added /quit latency for an idle session."""
    reg = _registry(tmp_path)
    await asyncio.wait_for(reg.shutdown(), timeout=1.0)
