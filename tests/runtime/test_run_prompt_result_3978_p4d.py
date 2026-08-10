"""Tier 2: proposal 0067 P4d (#3978) — session_api.run_prompt_result.

Real ``AgentRegistry`` + real ``Session`` (no mocks — mirrors
``test_deliver_cross_session_message_3978_p5.py``'s construction pattern).

Pins:

  1. No live target session → typed refusal (``target_session_not_found``),
     never a spawn (ADR-0040 D5 precedent).
  2. A target already self-running its own turn loop → typed refusal
     (``target_session_busy``), never driven inline (double-pump guard —
     architect's #3978 ruling, interim pending issue #4113).
  3. Two run_prompt calls, each ALREADY holding its OWN agent's
     ``get_agent_lock`` (simulating "this agent's own turn holds its lock",
     the real shape a live MCP/run_prompt caller is in) and each targeting
     the OTHER — a genuine mutual AB/BA lock-order deadlock. Both resolve
     via ``timeout`` (never hang), and neither error message claims a
     deadlock was DETECTED (architect's #3978 follow-up: no discriminator
     for "why" exists today — the message may name only WHAT was awaited).

Falsify-verified (pin 3): removing the ``asyncio.timeout(timeout)`` wrapper
around the lock acquisition in ``run_prompt_result`` (leaving only
``bus.request``'s own inner timeout, which never even runs here — the
deadlock is at the LOCK ACQUIRE, before ``bus.request`` is reached) made
THIS SAME test — including its own ``asyncio.wait_for(gather(...),
timeout=10.0)`` outer bound — hang past an external ``timeout 30`` shell
wrapper with no output at all (not even a failure report), confirming the
un-fixed lock acquisition is genuinely unresponsive to cancellation in this
shape, not merely slow. Restored afterward; green again (see run above).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.agent_locks import get_agent_lock
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_prompt_result
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_no_live_target_session_refuses_without_spawning(tmp_path):
    """Tier 2: ADR-0040 D5 precedent — a target naming no LIVE session is
    refused, never loaded/spawned (same posture send_to_session takes)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    reg.get_or_load("alpha")
    # "beta" is NEVER loaded — no live session exists for it.

    result = await run_prompt_result(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="hi", timeout=5.0,
    )

    assert result["status"] == "error"
    assert result["kind"] == "target_session_not_found"
    assert reg.get_session("beta", "main") is None, (
        "run_prompt(collect=\"attached\") must not have spawned a session for "
        "the never-loaded target"
    )


@pytest.mark.asyncio
async def test_self_running_target_refuses_rather_than_double_pump(tmp_path):
    """Tier 2: #4108-era double-pump guard — a target with a LIVE
    ``registry._tasks`` entry (``ensure_session_running``) is refused, never
    driven inline via MessageBus.request. Interim (architect's #3978
    ruling): AgentRegistry.is_session_running, pending issue #4113's durable
    replacement."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    reg.get_or_load("alpha")
    reg.get_or_load("beta")
    reg.ensure_session_running("beta", "main")
    try:
        assert reg.is_session_running("beta", "main"), (
            "test setup: beta must actually be self-running for this pin "
            "to test what it claims to"
        )

        result = await run_prompt_result(
            reg, caller_agent="alpha", caller_sid="main",
            target_agent="beta", target_session="main",
            prompt="hi", timeout=5.0,
        )

        assert result["status"] == "error"
        assert result["kind"] == "target_session_busy"
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_mutual_run_prompt_deadlock_resolves_via_timeout_not_a_hang(tmp_path):
    """Tier 2: the deadlock shape architect's #3978 correction named — A's
    "turn" holds lock(A) and calls run_prompt targeting B; B's "turn" holds
    lock(B) and calls run_prompt targeting A, concurrently. Each blocks
    trying to acquire the OTHER's lock while holding its own — classic
    AB/BA deadlock. ``asyncio.wait_for`` here is the TEST's own outer
    bound on how long it will wait for BOTH sides to settle (a normal
    assertion-time bound, not a workaround for an unbounded producer — the
    thing under test, ``run_prompt_result``, has its own REQUIRED
    ``timeout`` and settles well within it); it does not substitute for
    that production timeout, which is what this pin exists to witness."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    reg.get_or_load("alpha")
    reg.get_or_load("beta")

    # Both sides must have ALREADY acquired their own lock before EITHER
    # attempts the other's — otherwise this is a scheduling race, not a
    # guaranteed deadlock (an uncontended asyncio.Lock.acquire() does not
    # yield to the event loop, so without this rendezvous one side could
    # race all the way to bus.request before the other even starts).
    alpha_holds_lock = asyncio.Event()
    beta_holds_lock = asyncio.Event()

    async def alpha_side() -> dict:
        async with get_agent_lock("alpha", "main"):
            alpha_holds_lock.set()
            await beta_holds_lock.wait()
            return await run_prompt_result(
                reg, caller_agent="alpha", caller_sid="main",
                target_agent="beta", target_session="main",
                prompt="hi from alpha", timeout=1.0,
            )

    async def beta_side() -> dict:
        async with get_agent_lock("beta", "main"):
            beta_holds_lock.set()
            await alpha_holds_lock.wait()
            return await run_prompt_result(
                reg, caller_agent="beta", caller_sid="main",
                target_agent="alpha", target_session="main",
                prompt="hi from beta", timeout=1.0,
            )

    # This outer wait_for is currently INERT, not the active bound — the
    # falsify above measured that directly (removing production's
    # asyncio.timeout made the whole gather hang past an external
    # `timeout 30` shell wrapper, past this 10.0 too, with no output at
    # all). It stays as a safety net for a future change: if lock
    # acquisition ever becomes cancellable (asyncio.Lock's acquire()
    # honours cancellation today, but nothing here currently cancels it —
    # production's own asyncio.timeout is what unblocks each side), this
    # would then become the active bound instead. Do not read a green run
    # here as "the 10.0 is what's keeping this from hanging."
    results = await asyncio.wait_for(
        asyncio.gather(alpha_side(), beta_side()), timeout=10.0,
    )

    for result in results:
        assert result["status"] == "error"
        assert result["kind"] == "timeout"
        # architect's #3978 follow-up: no discriminator for WHY exists today
        # (CurrentTask.requester is always None at its one construction
        # site) — the message must not claim a deadlock was detected.
        assert "deadlock" not in result["error"].lower(), (
            "the error message must not assert a cause it cannot actually "
            "distinguish from an ordinary slow reply"
        )
