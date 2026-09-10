"""Tier 2: #6077 (backlog-watcher finding, part of that issue -- NOT its
own root cause) — ``Session._emit_router_cap_exhausted_user``'s own
``build_history()`` call was the one remaining call site on the router-cap
exhaustion path still running synchronously on the event loop; every other
``build_history()`` call on the normal submit path is already offloaded
via ``asyncio.to_thread`` (``router_loop_driver.py``'s own established
idiom). This pins that offload as a STATE fact — which function
``asyncio.to_thread`` was actually called with — never a measured
duration (CLAUDE.md: "a test writes no duration, in EITHER direction").

Real ``Session`` + a real, injectable wrapper around ``asyncio.to_thread``
(never a Mock) that records what it was called with before delegating to
the real implementation — the call genuinely still executes for real.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import LoopConfig, SafetyConfig
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.errors import RouterCapExceeded
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.router_loop import ScriptedLLM as _ScriptedLLM
from tests._support.router_loop import text_result


def _make_session(tmp_path: Path, cap: int = 3) -> Session:
    safety = SafetyConfig(loop=LoopConfig(max_router_calls_per_turn=cap))
    return make_session(
        agent_name="test_cap_offload_agent",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )


@pytest.mark.asyncio
async def test_build_history_is_offloaded_via_asyncio_to_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the load-bearing regression witness — ``build_history`` is
    reached through ``asyncio.to_thread``, the SAME idiom
    ``router_loop_driver.py``'s own submit-path call already uses, not a
    direct synchronous call on the event loop.

    Strip: change ``await asyncio.to_thread(self._history_buffer.
    build_history)`` back to ``self._history_buffer.build_history()`` in
    ``Session._emit_router_cap_exhausted_user`` -- this goes RED (the
    wrapper below never sees ``build_history`` as a target, performed
    during review)."""
    session = _make_session(tmp_path)
    calls: "list[object]" = []
    real_to_thread = asyncio.to_thread

    async def _recording_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _recording_to_thread)

    llm = _ScriptedLLM([text_result("wrap-up summary")])
    exc = RouterCapExceeded(count=3, cap=3, last_reason="loop_reason")

    await session._emit_router_cap_exhausted_user(
        exc, chain_id="chain-cap-offload", _llm_caller=llm,
    )

    # Identify the target by its OWN introspectable identity (name +
    # owning class), never by reaching through session._history_buffer at
    # the assertion site — the wrapper above is the public witness; what
    # it recorded is what this checks.
    assert any(
        getattr(fn, "__name__", None) == "build_history"
        and type(getattr(fn, "__self__", None)).__name__ == "RouterHistoryBuffer"
        for fn in calls
    ), (
        f"expected build_history (RouterHistoryBuffer's own) to be reached "
        f"via asyncio.to_thread, got these targets instead: {calls!r}"
    )
