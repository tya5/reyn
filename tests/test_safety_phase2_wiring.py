"""Tier 2 invariants for FP-0005 Phase 2 — per-site safety-limit wiring.

These tests pin the contract that each of the six safety-limit raise
sites consults ``handle_limit_exceeded`` before raising. They exercise
the code path end-to-end with a stub ``InterventionBus`` (= no real
LLM, no real workspace) and verify both the legacy abort path
(``mode=unattended``) and the auto_extend / interactive paths.

Sites covered here:
  - B (max_phase_visits) — ``OSRuntime._enter_phase``
  - C (router_cap)        — ``Session._check_and_increment_router_cap``

The remaining sites (F phase_seconds, A max_act_turns, E max_hop_depth,
G chain_seconds) share the same helper and are exercised indirectly
via ``test_safety_limit_handler.py``'s helper-level invariants. Per-
site smoke coverage for those would be valuable follow-up if specific
bugs surface.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.kernel.runtime import LoopLimitExceededError, OSRuntime
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.limits.limit_handler import reset_run_extensions
from reyn.runtime.session import RouterCapExceeded, Session
from reyn.schemas.models import Phase, Skill, SkillGraph


def _one_phase_skill() -> Skill:
    """Mirror tests/test_runtime_llm_memoization.py::_one_phase_skill."""
    p = Phase(
        name="greet",
        instructions="greet",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="ping",
        entry_phase="greet",
        phases={"greet": p},
        graph=SkillGraph(transitions={"greet": ["greet"]}, can_finish_phases=["greet"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="greeting",
    )


# ─── B (max_phase_visits) — OSRuntime._enter_phase ─────────────────────


@pytest.mark.asyncio
async def test_os_max_phase_visits_unattended_raises_on_hit() -> None:
    """Tier 2: explicit ``mode=unattended`` raises ``LoopLimitExceededError``
    on hit without dispatching any intervention. Default mode is now
    ``interactive``, so opting into legacy abort-on-hit behaviour is
    explicit (= the ``OnLimitConfig(mode="unattended")`` override).
    """
    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="run-fp5-B-unatt",
        safety=SafetyConfig(on_limit=OnLimitConfig(mode="unattended")),
    )
    rt._max_phase_visits = 2

    # Visit twice — fine.
    await rt._enter_phase("greet", {"type": "greet_in", "data": {}})
    await rt._enter_phase("greet", {"type": "greet_in", "data": {}})

    # Third entry: cap hit. Unattended mode → raise.
    with pytest.raises(LoopLimitExceededError) as excinfo:
        await rt._enter_phase("greet", {"type": "greet_in", "data": {}})
    assert "max_phase_visits" in str(excinfo.value)


@pytest.mark.asyncio
async def test_os_max_phase_visits_auto_extend_grants_then_aborts() -> None:
    """Tier 2: ``mode=auto_extend`` lets the phase be entered
    ``auto_extend_times`` more times after the cap, then raises.
    No bus is consulted (auto_extend is silent).
    """
    reset_run_extensions("run-fp5-B-auto")
    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="run-fp5-B-auto",
        safety=SafetyConfig(on_limit=OnLimitConfig(mode="auto_extend", auto_extend_times=1)),
    )
    rt._max_phase_visits = 1

    # First visit OK.
    await rt._enter_phase("greet", {"type": "greet_in", "data": {}})

    # Second visit: cap hit, but auto_extend grants once → succeeds.
    await rt._enter_phase("greet", {"type": "greet_in", "data": {}})

    # Third visit: extension exhausted → raise.
    with pytest.raises(LoopLimitExceededError):
        await rt._enter_phase("greet", {"type": "greet_in", "data": {}})


# ─── C (router_cap) — Session._check_and_increment_router_cap ──────


def _make_session(*, cap: int, on_limit: OnLimitConfig) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(max_router_calls_per_turn=cap),
        on_limit=on_limit,
    )
    return Session(
        agent_name="test_agent",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )


def test_router_cap_unattended_raises_on_hit(tmp_path, monkeypatch) -> None:
    """Tier 2: with explicit ``unattended`` mode, the router cap
    fires the legacy ``RouterCapExceeded`` raise — byte-for-byte
    legacy behaviour. Default mode is now ``interactive``; opting
    into immediate-raise is the ``OnLimitConfig(mode="unattended")``
    override.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(cap=2, on_limit=OnLimitConfig(mode="unattended"))
    session._reset_router_turn_counter()

    # Two invocations OK.
    asyncio.run(session._check_and_increment_router_cap("a"))
    asyncio.run(session._check_and_increment_router_cap("b"))

    # Third raises.
    with pytest.raises(RouterCapExceeded):
        asyncio.run(session._check_and_increment_router_cap("c"))


def test_router_cap_auto_extend_grants_then_aborts(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``mode=auto_extend`` extends the cap on hit, then
    aborts when the auto_extend budget is exhausted. Each extension
    is +1 invocation by default.
    """
    monkeypatch.chdir(tmp_path)
    reset_run_extensions("test_agent")
    session = _make_session(
        cap=1,
        on_limit=OnLimitConfig(mode="auto_extend", auto_extend_times=2),
    )
    session._reset_router_turn_counter()

    # First call OK (within original cap=1).
    asyncio.run(session._check_and_increment_router_cap("a"))
    # Second hit: original cap exceeded; auto_extend grant #1 → cap becomes 2.
    asyncio.run(session._check_and_increment_router_cap("b"))
    # Third hit: cap=2 exceeded; auto_extend grant #2 → cap becomes 3.
    asyncio.run(session._check_and_increment_router_cap("c"))
    # Fourth hit: budget exhausted → raise.
    with pytest.raises(RouterCapExceeded):
        asyncio.run(session._check_and_increment_router_cap("d"))


# ─── Session threads on_limit through the constructor ─────────────


def test_chatsession_default_on_limit_is_interactive() -> None:
    """Tier 2: a Session without an explicit ``safety`` argument
    defaults to ``interactive`` on_limit so a TUI / a2a run holds open
    for a user reply rather than silently discarding mid-run state.
    See ``OnLimitConfig`` docstring for the headless safety story.
    """
    s = Session(agent_name="t")
    assert s.on_limit.mode == "interactive"


def test_chatsession_threads_on_limit_through_constructor() -> None:
    """Tier 2: an explicit ``on_limit`` threaded through ``safety`` is
    honoured. This is the path used by the CLI factory
    (``cli/commands/chat.py``, ``web/deps.py``, ``cli/commands/mcp.py``)
    which passes ``config.safety`` from the loaded ReynConfig.
    """
    on_limit = OnLimitConfig(mode="interactive", auto_extend_times=5)
    s = Session(agent_name="t", safety=SafetyConfig(on_limit=on_limit))
    assert s.on_limit is on_limit
    assert s.on_limit.mode == "interactive"
    assert s.on_limit.auto_extend_times == 5
