"""Tier 2: programmatic run_skill spawn budget-exceed honours on_limit (#1880).

#1880 unifies the programmatic spawn path (``SkillRunner.run_skill_awaitable``,
the ``:592`` gate) with the chat-spawn path (``:268``, #1877): a per-chain budget
hard-hit on a dimension with a configured extension routes to
``ask_budget_extension`` (interactive=ask / auto_extend=bounded / unattended=deny,
decided inside that helper — programmatic spawns have no TTY → bus None → the
interactive branch falls to ``no_bus`` deny, the existing fail-closed) instead of
the old unconditional hard-refuse. ``extension_calls == 0`` (default) short-circuits
to the hard refusal → **default-config behavior is byte-identical**.

Policy: real ``SkillRunner`` + real ``BudgetCheck``; the ask boundary is a recording
async fn (mirrors test_ask_budget_extension_on_limit_1877). No mocks of collaborators.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.runtime.budget.budget import BudgetCheck
from reyn.skill.skill_runner import SkillRunner


class _RefusingBudget:
    """check_pre_spawn always refuses (configurable extension_calls); records calls."""

    def __init__(self, extension_calls: int) -> None:
        self._ext = extension_calls
        self.record_spawn_calls: list = []
        self.extend_calls: list = []

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> BudgetCheck:
        return BudgetCheck(
            allowed=False, hard_dimension="per_chain_skill_calls", detail="cap hit",
            context={
                "skill": skill, "chain_id": chain_id, "current": 1, "hard": 1,
                "base_hard": 1, "extensions_granted": 0, "extension_calls": self._ext,
            },
        )

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        self.record_spawn_calls.append((chain_id, skill))

    def extend_chain_calls(self, *, chain_id: str, skill: str, additional: int) -> int:
        self.extend_calls.append((chain_id, skill, additional))
        return additional


class _ExtendableBudget(_RefusingBudget):
    """Refuses until extend_chain_calls is called, then allows (the approve path)."""

    def __init__(self, extension_calls: int) -> None:
        super().__init__(extension_calls)
        self._extended = False

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> BudgetCheck:
        if self._extended:
            return BudgetCheck(allowed=True)
        return super().check_pre_spawn(chain_id=chain_id, skill=skill)

    def extend_chain_calls(self, *, chain_id: str, skill: str, additional: int) -> int:
        self._extended = True
        return super().extend_chain_calls(chain_id=chain_id, skill=skill, additional=additional)


def _make_runner(*, budget, ask_returns: bool) -> tuple[SkillRunner, list]:
    ask_calls: list = []

    async def _ask_budget_extension(**kwargs) -> bool:
        ask_calls.append(kwargs)
        return ask_returns

    async def _put_outbox(msg) -> None:
        pass

    async def _enqueue_completed(**kwargs) -> None:
        pass

    runner = SkillRunner(
        event_log=EventLog(),
        agent_name="test_agent",
        output_language=None,
        mcp_servers=None,
        allowed_skills=None,
        budget=budget,
        state_log=None,
        build_agent_fn=lambda run_id, skill_name, *, subscribers=None: None,
        put_outbox=_put_outbox,
        enqueue_skill_completed=_enqueue_completed,
        accumulate=lambda result: None,
        drop_interventions_for_run=lambda run_id: None,
        get_skill_registry=lambda: None,
        ask_budget_extension=_ask_budget_extension,
        make_subscribers=lambda skill_name, run_id=None: [],
        format_refusal=lambda check: "refused",
        format_warn=lambda dim, ctx: "warn",
    )
    return runner, ask_calls


async def _run_gate(runner) -> dict:
    """Drive run_skill_awaitable past the budget gate; swallow any downstream
    skill-load error (the harness has no real skill — we assert on the gate only)."""
    try:
        return await runner.run_skill_awaitable({"skill": "s", "input": {}}, chain_id="c1")
    except Exception:  # noqa: BLE001 — downstream skill-run is out of scope
        return {"status": "error", "data": {"error": "downstream"}}


@pytest.mark.asyncio
async def test_default_extension_calls_zero_hard_refuse_unchanged():
    """Tier 2: extension_calls==0 (default) → hard refuse, ask NOT called (the
    default-config path is unchanged from pre-#1880)."""
    budget = _RefusingBudget(extension_calls=0)
    runner, ask_calls = _make_runner(budget=budget, ask_returns=True)
    result = await _run_gate(runner)
    assert ask_calls == [], "extension_calls==0 must short-circuit (no ask) — byte-identical"
    assert result["status"] == "error" and not budget.record_spawn_calls


@pytest.mark.asyncio
async def test_exceed_with_extension_routes_to_ask():
    """Tier 2: exceed + extension_calls>0 → the programmatic gate routes to the
    unified ask flow (the #1880 unification with :268)."""
    runner, ask_calls = _make_runner(budget=_RefusingBudget(extension_calls=3), ask_returns=False)
    await _run_gate(runner)
    assert ask_calls, "extension_calls>0 must route the exceed to ask_budget_extension"


@pytest.mark.asyncio
async def test_declined_refuses_no_spawn():
    """Tier 2: ask declines (= unattended / interactive-no / programmatic no_bus
    deny outcome) → hard refuse, no spawn recorded."""
    budget = _RefusingBudget(extension_calls=3)
    runner, _ = _make_runner(budget=budget, ask_returns=False)
    result = await _run_gate(runner)
    assert result["status"] == "error" and not budget.record_spawn_calls


@pytest.mark.asyncio
async def test_approved_extends_and_proceeds():
    """Tier 2: ask approves (= auto_extend / interactive-yes) → extend the cap +
    proceed past the gate (record_spawn fires; the call is no longer refused)."""
    budget = _ExtendableBudget(extension_calls=3)
    runner, ask_calls = _make_runner(budget=budget, ask_returns=True)
    await _run_gate(runner)
    assert ask_calls, "approve path must have routed to ask"
    assert budget.extend_calls == [("c1", "s", 3)], "approval must extend the chain cap"
    assert budget.record_spawn_calls == [("c1", "s")], "after extend, the spawn proceeds"
