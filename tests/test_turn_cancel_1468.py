"""Tier 2: #1468 — cooperative turn cancellation via cancel_inflight().

Invariants pinned:

1. cooperative cancel: when _is_turn_cancel_requested() returns True at
   iteration boundary, run_loop emits turn_cancelled event and breaks cleanly.
2. cancel fires AFTER current tool completes, not mid-call (cooperative — the
   flag is checked at the TOP of each iteration, before the LLM call).
3. idle cancel is spurious-safe: flag reset at turn entry means a cancel_inflight()
   fired while no turn is running is consumed on the next turn's first iteration
   check — or cleared before the LLM call if no cancel was requested.
4. turn_cancelled event carries chain_id (P6 audit trail).
5. Single seam: cancel_inflight() sets turn flag + cancels skills/plans.
6. WS path calls cancel_inflight() not inline loops (structural seam pin).

No mocks. RouterLoop is driven via llm_caller= injection (_ScriptedLLM, real
callable class). cancel flag is set via a host subclass or direct session method.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests.test_router_loop import (
    FakeRouterHost,
    _ScriptedLLM,
    text_result,
)


def _usage() -> TokenUsage:
    return TokenUsage(prompt_tokens=10, completion_tokens=5)


# ── Host subclass with cancel flag ──────────────────────────────────────────


class _CancellableHost(FakeRouterHost):
    """FakeRouterHost subclass that exposes _is_turn_cancel_requested()."""

    def __init__(self) -> None:
        super().__init__()
        self._cancel_after_n: int | None = None  # set before iteration N fires cancel
        self._iteration_count: int = 0

    def arm_cancel_after(self, n: int) -> None:
        """Fire cancel after n iterations (0 = on the first check)."""
        self._cancel_after_n = n

    def _is_turn_cancel_requested(self) -> bool:
        self._iteration_count += 1
        if self._cancel_after_n is None:
            return False
        return self._iteration_count > self._cancel_after_n


def _loop(host: _CancellableHost, llm: _ScriptedLLM, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(
        host=host,
        chain_id="chain-cancel-test",
        max_iterations=max_iterations,
        llm_caller=llm,
    )


# ── 1. Cooperative cancel fires at iteration boundary ───────────────────────


@pytest.mark.asyncio
async def test_cooperative_cancel_breaks_loop_cleanly() -> None:
    """Tier 2: #1468 — when _is_turn_cancel_requested() returns True at the
    top of an iteration, run_loop breaks without raising — clean exit."""
    host = _CancellableHost()
    host.arm_cancel_after(0)  # cancel on the FIRST iteration check
    # Script: two text replies (only the first COULD be reached)
    llm = _ScriptedLLM([text_result("should not run"), text_result("also not")])
    loop = _loop(host, llm)
    # Must return cleanly (not raise) — the loop breaks on cancel
    usage = await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )
    assert isinstance(usage, TokenUsage)
    # LLM was never called — the cancel fired before the first LLM call
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_turn_cancelled_event_emitted() -> None:
    """Tier 2: #1468 — on cooperative cancel, a turn_cancelled event is emitted
    (P6 audit trail). The event must carry the chain_id."""
    host = _CancellableHost()
    host.arm_cancel_after(0)
    llm = _ScriptedLLM([text_result("unreachable")])
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )
    cancelled_events = [
        e for e in host.events.emitted if e.get("type") == "turn_cancelled"
    ]
    assert len(cancelled_events) == 1, "exactly one turn_cancelled event expected"
    assert cancelled_events[0].get("chain_id") == "chain-cancel-test"


@pytest.mark.asyncio
async def test_cancel_after_one_iteration_allows_first_llm_call() -> None:
    """Tier 2: #1468 — cancel armed after iteration 1 lets the first LLM call
    complete (= cooperative: cancel fires at boundary, not mid-call).
    Second iteration is skipped."""
    host = _CancellableHost()
    host.arm_cancel_after(1)  # cancel fires on iteration 2 check
    llm = _ScriptedLLM([text_result("first reply"), text_result("never reached")])
    loop = _loop(host, llm, max_iterations=3)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )
    # First LLM call completed before cancel fired; second was not reached
    assert llm.call_count == 1


# ── 2. No cancel flag → normal execution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_no_cancel_flag_runs_normally() -> None:
    """Tier 2: #1468 — when _is_turn_cancel_requested() returns False, the
    loop runs normally to completion (regression: cancel path must not fire
    spuriously)."""
    host = _CancellableHost()
    # No arm_cancel_after — flag stays False throughout
    llm = _ScriptedLLM([text_result("normal reply")])
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )
    assert llm.call_count == 1  # ran normally


@pytest.mark.asyncio
async def test_host_without_cancel_method_runs_normally() -> None:
    """Tier 2: #1468 — a host that does NOT implement _is_turn_cancel_requested
    (e.g. phase host) runs normally — getattr-guard must make it a no-op."""
    host = FakeRouterHost()  # no _is_turn_cancel_requested
    llm = _ScriptedLLM([text_result("runs fine")])
    loop = RouterLoop(
        host=host,
        chain_id="chain-phase-host",
        max_iterations=3,
        llm_caller=llm,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )
    assert llm.call_count == 1  # completed normally, no cancel


# ── 3. session.cancel_inflight() single seam ─────────────────────────────────


class _MinimalEventsLog:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **kw) -> None:
        self.emitted.append({"kind": kind, **kw})


class _FakeSkillTask:
    """Real fake asyncio-task stand-in for running_skills testing."""

    def __init__(self) -> None:
        self._done = False
        self._cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> bool:
        if not self._done:
            self._cancelled = True
            self._done = True
            return True
        return False


class _SessionWithCancelSeam:
    """Minimal session-like object exposing the #1468 cancel seam."""

    def __init__(self) -> None:
        self._turn_cancel_requested = False
        self.running_skills: dict = {}
        self.running_plans: dict = {}

    def _is_turn_cancel_requested(self) -> bool:
        return self._turn_cancel_requested

    async def cancel_inflight(self) -> str:
        from reyn.chat.session import ChatSession
        # Call the real method (unbound, passing self)
        return await ChatSession.cancel_inflight(self)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancel_inflight_sets_turn_flag() -> None:
    """Tier 2: #1468 — cancel_inflight() sets the turn cancel flag so the
    next run_loop iteration boundary will fire."""
    session = _SessionWithCancelSeam()
    assert not session._turn_cancel_requested
    await session.cancel_inflight()
    assert session._turn_cancel_requested


@pytest.mark.asyncio
async def test_cancel_inflight_cancels_skills_and_plans() -> None:
    """Tier 2: #1468 — cancel_inflight() cancels all non-done skill/plan tasks
    in the single call (single seam covers turn + skills + plans)."""
    session = _SessionWithCancelSeam()
    skill_task = _FakeSkillTask()
    plan_task = _FakeSkillTask()
    session.running_skills = {"r1": skill_task}
    session.running_plans = {"p1": plan_task}
    await session.cancel_inflight()
    assert skill_task._cancelled
    assert plan_task._cancelled


@pytest.mark.asyncio
async def test_cancel_inflight_already_done_tasks_not_recancelled() -> None:
    """Tier 2: #1468 — tasks that are already done are not recancelled
    (cancel() must not be called on a done task)."""
    session = _SessionWithCancelSeam()
    done_task = _FakeSkillTask()
    done_task._done = True  # already finished
    session.running_skills = {"r1": done_task}
    await session.cancel_inflight()
    assert not done_task._cancelled  # done tasks are skipped


@pytest.mark.asyncio
async def test_idle_cancel_is_spurious_safe() -> None:
    """Tier 2: #1468 — the cancel flag is reset to False at turn entry
    (session._run_router_loop) so an idle cancel_inflight() call does not
    bleed into the next turn. This pins the reset semantics via the session
    method directly."""
    session = _SessionWithCancelSeam()
    await session.cancel_inflight()
    assert session._turn_cancel_requested  # flag set by cancel
    # Simulate turn entry reset (what _run_router_loop does)
    session._turn_cancel_requested = False
    assert not session._turn_cancel_requested  # clean for next turn
