"""Tier 2: OS-invariant tests for #1868 — budget-exceed → 3-mode limit framework.

The per-LLM-call cost gate no longer hard-denies unconditionally: a
``check_pre_llm`` refusal routes through ``handle_limit_exceeded``
(deny / auto-allow / ask-user), reusing ``safety.on_limit`` (lead decision A). The
policy context (bus / on_limit / run_id / non_interactive) is published by the
runtime via ``set_budget_limit_context``; **UNSET → fail-closed deny** (safety-
critical: no policy = no silent allow).

Mandatory gates (lead): (a) fail-closed non-tty, (b) budget-iv falsification
(exceed→ask→yes=allow / no=deny), (c) unset-context fail-closed. (d) replay OFF
byte-identical + budget=None inert is covered by the replay suite + the unchanged
``if budget is not None`` guard (structural).

Policy: real ``BudgetCheck`` + real ``handle_limit_exceeded`` + real
``OnLimitConfig`` + real ``set_budget_limit_context``; the bus is a minimal fake
(it IS the user-ask boundary, mirroring test_safety_limit_handler). Tier line first.
"""
from __future__ import annotations

import pytest

import reyn.llm.llm as llm_mod
from reyn.config.chat import OnLimitConfig
from reyn.llm.llm import _budget_exceed_allows_continue, set_budget_limit_context
from reyn.runtime.budget.budget import BudgetCheck
from reyn.user_intervention import InterventionAnswer


@pytest.fixture(autouse=True)
def _reset_budget_ctx():
    llm_mod._budget_limit_context_var.set(None)
    yield
    llm_mod._budget_limit_context_var.set(None)


class _Bus:
    """Minimal RequestBus fake (mirrors test_safety_limit_handler._FakeBus)."""

    def __init__(self, choice: str | None) -> None:
        self._choice = choice
        self.asked = False
        self.last_kind: str | None = None

    async def request(self, iv) -> InterventionAnswer:  # type: ignore[no-untyped-def]
        self.asked = True
        self.last_kind = getattr(iv, "kind", None)
        return InterventionAnswer(text="", choice_id=self._choice)


def _refusal() -> BudgetCheck:
    return BudgetCheck(allowed=False, hard_dimension="daily_cost_usd", detail="cap reached")


# (c) unset-context fail-closed ---------------------------------------------------

@pytest.mark.asyncio
async def test_unset_context_fails_closed() -> None:
    """Tier 2: no policy context published → the gate fails CLOSED (deny). A budget
    exceed with no runtime context must never silently allow."""
    allowed = await _budget_exceed_allows_continue(_refusal(), "agent-a")
    assert allowed is False


# (b) budget-iv falsification -----------------------------------------------------

@pytest.mark.asyncio
async def test_interactive_yes_allows() -> None:
    """Tier 2: interactive + user says YES → the over-budget call is allowed
    (owner intent: 予算到達時も iv による継続判断)."""
    bus = _Bus("yes")
    set_budget_limit_context(bus, OnLimitConfig(mode="interactive"), "run-yes", False)
    assert await _budget_exceed_allows_continue(_refusal(), "agent-a") is True
    assert bus.asked and bus.last_kind == "safety.limit.cost.daily_cost_usd", (
        "the budget exceed must reach the user as a cost-kind intervention"
    )


@pytest.mark.asyncio
async def test_interactive_no_denies() -> None:
    """Tier 2: (falsification) interactive + user says NO → denied. If the gate
    ignored the answer this would wrongly allow."""
    bus = _Bus("no")
    set_budget_limit_context(bus, OnLimitConfig(mode="interactive"), "run-no", False)
    assert await _budget_exceed_allows_continue(_refusal(), "agent-a") is False


@pytest.mark.asyncio
async def test_unattended_denies() -> None:
    """Tier 2: unattended mode → immediate deny (today's default preserved)."""
    bus = _Bus("yes")  # would say yes, but unattended never asks
    set_budget_limit_context(bus, OnLimitConfig(mode="unattended"), "run-un", False)
    assert await _budget_exceed_allows_continue(_refusal(), "agent-a") is False
    assert bus.asked is False


# (a) fail-closed non-tty ---------------------------------------------------------

@pytest.mark.asyncio
async def test_non_interactive_bounded_no_bus_call() -> None:
    """Tier 2: interactive mode but non-tty (non_interactive=True) → it must NOT
    hang on the bus and must be BOUNDED. With auto_extend_times=0 it denies
    immediately; the bus is never asked (no TTY to ask)."""
    bus = _Bus("yes")
    set_budget_limit_context(
        bus, OnLimitConfig(mode="interactive", auto_extend_times=0), "run-nitty", True,
    )
    allowed = await _budget_exceed_allows_continue(_refusal(), "agent-a")
    assert allowed is False, "non-tty interactive with 0 extensions must deny (bounded)"
    assert bus.asked is False, "non-tty must never dispatch to the bus (no hang)"


@pytest.mark.asyncio
async def test_auto_extend_bounded() -> None:
    """Tier 2: auto_extend allows up to auto_extend_times then denies (bounded
    auto-allow), per-(run_id, kind)."""
    set_budget_limit_context(
        _Bus(None), OnLimitConfig(mode="auto_extend", auto_extend_times=1), "run-ax", False,
    )
    first = await _budget_exceed_allows_continue(_refusal(), "agent-a")
    second = await _budget_exceed_allows_continue(_refusal(), "agent-a")
    assert first is True and second is False, "auto_extend is bounded (1 allow, then deny)"
