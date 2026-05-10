"""Tier 2 invariants for FP-0005 Phase 2 — the shared
``handle_limit_exceeded`` helper.

This is the architectural contract that all six per-site call sites
(max_phase_visits, phase_seconds, max_act_turns, router_cap,
max_hop_depth, chain_seconds) consume. FP-0003's
``_ask_budget_extension`` is generalised to call into this helper as
well.

These tests pin the helper's behaviour in isolation. Per-site wiring
tests (= "site B actually consults the helper before raising") live
alongside their respective call sites' invariant suites.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config import OnLimitConfig
from reyn.safety.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    UserIntervention,
)


class _FakeBus:
    """Minimal InterventionBus impl that resolves a queued answer.

    Records every dispatched intervention so tests can assert on the
    prompt text / kind / choices.
    """

    def __init__(self, answer_choice: str | None) -> None:
        self._answer_choice = answer_choice
        self.dispatched: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.dispatched.append(iv)
        return InterventionAnswer(text="", choice_id=self._answer_choice)


class _HangingBus:
    """Bus that never resolves — used to test ask_timeout."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        await asyncio.Event().wait()  # blocks forever
        raise AssertionError("unreachable")  # pragma: no cover


class _RaisingBus:
    """Bus that raises on request — used to test the bus-failure refusal path."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        raise RuntimeError("bus disconnected")


# ─── unattended mode (= legacy abort) ────────────────────────────────


@pytest.mark.asyncio
async def test_unattended_mode_returns_refused_immediately() -> None:
    """Tier 2: ``mode=unattended`` (= default) returns
    ``allow_continue=False`` without dispatching any intervention.
    Legacy callers + CI/scripted runs preserve their abort-on-hit
    behaviour byte-for-byte.
    """
    bus = _FakeBus(answer_choice="yes")  # would say yes, but never asked
    decision = await handle_limit_exceeded(
        bus=bus,
        on_limit=OnLimitConfig(mode="unattended"),
        kind="max_phase_visits",
        run_id="run-A",
        prompt="?",
    )
    assert decision == LimitDecision(
        allow_continue=False, extension=0.0, reason="unattended",
    )
    assert bus.dispatched == []  # no intervention dispatched


# ─── interactive mode ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interactive_yes_allows_continue_with_extension() -> None:
    """Tier 2: ``mode=interactive`` + user picks ``yes`` → caller is
    told to continue with the requested ``extension_amount``.
    """
    bus = _FakeBus(answer_choice="yes")
    decision = await handle_limit_exceeded(
        bus=bus,
        on_limit=OnLimitConfig(mode="interactive"),
        kind="router_cap",
        run_id="run-B",
        prompt="Router hit cap of 3 — continue?",
        extension_amount=1.0,
    )
    assert decision.allow_continue is True
    assert decision.extension == 1.0
    assert decision.reason == "user_approved"
    # The dispatched intervention carries the namespaced kind.
    assert len(bus.dispatched) == 1
    assert bus.dispatched[0].kind == "safety.limit.router_cap"


@pytest.mark.asyncio
async def test_interactive_no_returns_refusal() -> None:
    """Tier 2: ``mode=interactive`` + user picks ``no`` →
    ``allow_continue=False`` with reason ``user_refused``. Caller
    falls through to legacy abort.
    """
    bus = _FakeBus(answer_choice="no")
    decision = await handle_limit_exceeded(
        bus=bus,
        on_limit=OnLimitConfig(mode="interactive"),
        kind="max_phase_visits",
        run_id="run-C",
        prompt="?",
    )
    assert decision.allow_continue is False
    assert decision.reason == "user_refused"


@pytest.mark.asyncio
async def test_interactive_unrecognised_choice_treated_as_refusal() -> None:
    """Tier 2: ``choice_id`` = anything other than ``"yes"`` (None,
    free-text mismatch, future labels) is treated as refusal. Mirrors
    FP-0003's defensive behaviour.
    """
    bus = _FakeBus(answer_choice=None)  # no match
    decision = await handle_limit_exceeded(
        bus=bus,
        on_limit=OnLimitConfig(mode="interactive"),
        kind="phase_seconds",
        run_id="run-D",
        prompt="?",
    )
    assert decision.allow_continue is False
    assert decision.reason == "user_refused"


@pytest.mark.asyncio
async def test_interactive_no_bus_returns_no_bus_reason() -> None:
    """Tier 2: ``bus=None`` in interactive mode = no UX surface →
    fall through with reason ``no_bus`` rather than hanging. Pins the
    headless-runtime contract.
    """
    decision = await handle_limit_exceeded(
        bus=None,
        on_limit=OnLimitConfig(mode="interactive"),
        kind="max_act_turns",
        run_id="run-E",
        prompt="?",
    )
    assert decision.allow_continue is False
    assert decision.reason == "no_bus"


@pytest.mark.asyncio
async def test_interactive_ask_timeout_returns_refusal() -> None:
    """Tier 2: when ``ask_timeout_seconds`` elapses without a reply
    the helper returns ``ask_timeout``. The hung bus is cancelled by
    ``asyncio.wait_for`` underneath.
    """
    bus = _HangingBus()
    decision = await handle_limit_exceeded(
        bus=bus,
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.05),
        kind="chain_seconds",
        run_id="run-F",
        prompt="?",
    )
    assert decision.allow_continue is False
    assert decision.reason == "ask_timeout"


@pytest.mark.asyncio
async def test_interactive_bus_exception_treated_as_refusal() -> None:
    """Tier 2: a bus that raises on ``request`` is treated as a refusal
    (= fail closed). Failing open here would let limit hits silently
    bypass via a flaky bus.
    """
    decision = await handle_limit_exceeded(
        bus=_RaisingBus(),
        on_limit=OnLimitConfig(mode="interactive"),
        kind="max_hop_depth",
        run_id="run-G",
        prompt="?",
    )
    assert decision.allow_continue is False
    assert decision.reason == "user_refused"


# ─── auto_extend mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_extend_grants_up_to_budget_then_aborts() -> None:
    """Tier 2: ``mode=auto_extend`` grants approvals up to
    ``auto_extend_times``; subsequent hits fall through to abort.
    No bus is ever consulted.
    """
    reset_run_extensions("run-H")
    bus = _FakeBus(answer_choice="yes")  # would say yes, but never asked
    on_limit = OnLimitConfig(mode="auto_extend", auto_extend_times=2)

    # First two hits auto-extended.
    for i in range(2):
        d = await handle_limit_exceeded(
            bus=bus, on_limit=on_limit,
            kind="max_phase_visits", run_id="run-H",
            prompt="?", extension_amount=5.0,
        )
        assert d.allow_continue is True, f"iteration {i}"
        assert d.extension == 5.0
        assert d.reason == "auto_extended"

    # Third hit exhausts the budget.
    d = await handle_limit_exceeded(
        bus=bus, on_limit=on_limit,
        kind="max_phase_visits", run_id="run-H",
        prompt="?",
    )
    assert d.allow_continue is False
    assert d.reason == "unattended"
    assert bus.dispatched == []  # bus never consulted in auto_extend mode


@pytest.mark.asyncio
async def test_auto_extend_bookkeeping_per_run_per_kind() -> None:
    """Tier 2: the auto_extend counter is keyed on ``(run_id, kind)`` —
    two different limits in the same run, or the same limit in two
    different runs, each get a fresh budget.
    """
    reset_run_extensions("run-I")
    reset_run_extensions("run-J")
    on_limit = OnLimitConfig(mode="auto_extend", auto_extend_times=1)

    # run-I, kind X → grant
    d = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="max_phase_visits", run_id="run-I", prompt="?",
    )
    assert d.allow_continue is True

    # run-I, kind Y → grant (different kind)
    d = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="router_cap", run_id="run-I", prompt="?",
    )
    assert d.allow_continue is True

    # run-J, kind X → grant (different run)
    d = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="max_phase_visits", run_id="run-J", prompt="?",
    )
    assert d.allow_continue is True

    # run-I, kind X again → exhausted
    d = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="max_phase_visits", run_id="run-I", prompt="?",
    )
    assert d.allow_continue is False


@pytest.mark.asyncio
async def test_reset_run_extensions_clears_all_kinds_for_run() -> None:
    """Tier 2: ``reset_run_extensions(run)`` drops every
    ``(run, *)`` entry, but leaves other runs' counters intact.
    Called at run boundaries so a fresh run gets a fresh budget.
    """
    reset_run_extensions("run-K")
    reset_run_extensions("run-L")
    on_limit = OnLimitConfig(mode="auto_extend", auto_extend_times=1)

    # Use run-K's budget for two kinds.
    await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="kind-1", run_id="run-K", prompt="?",
    )
    await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="kind-2", run_id="run-K", prompt="?",
    )
    # Use run-L's budget too.
    await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="kind-1", run_id="run-L", prompt="?",
    )
    # Reset run-K only.
    reset_run_extensions("run-K")

    # run-K kind-1 should be granted (fresh budget); run-L kind-1 should NOT (used).
    d_k = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="kind-1", run_id="run-K", prompt="?",
    )
    d_l = await handle_limit_exceeded(
        bus=None, on_limit=on_limit,
        kind="kind-1", run_id="run-L", prompt="?",
    )
    assert d_k.allow_continue is True
    assert d_l.allow_continue is False


# ─── InterventionBus protocol compliance ──────────────────────────────


def test_fake_bus_satisfies_intervention_bus_protocol() -> None:
    """Tier 1 framework boundary: ``_FakeBus`` is structurally
    compatible with ``InterventionBus`` so the helper's typed
    parameter doesn't drift.
    """
    assert isinstance(_FakeBus(answer_choice="yes"), InterventionBus)
