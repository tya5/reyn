"""Tier 2: per_chain_skill_calls exceed is driven by safety.on_limit.mode (#1877).

#1877 subsumed the per-dimension ``CostLimitConfig.ask_on_exceed`` bool into the
unified ``safety.on_limit`` 3-mode policy (clean-break). Two seams:

  Site 2 — ``Session._ask_budget_extension``: now passes the **real**
  ``self._on_limit`` to ``handle_limit_exceeded`` (it previously hardcoded
  ``mode="interactive"``, over-prompting even under ``on_limit=unattended`` in
  CI/cron — the bug this fixes). interactive → ask; unattended → deny WITHOUT
  prompting; auto_extend → bounded auto-grant WITHOUT prompting.

  Site 1 — ``SkillRunner`` spawn gate: the exceed routes to
  ``ask_budget_extension`` when ``extension_calls > 0`` (the participation
  signal that replaced ``ask_on_exceed``); ``extension_calls == 0`` (default)
  short-circuits to a hard refusal.

Policy: real Session + real ``handle_limit_exceeded`` + real ``OnLimitConfig``;
the intervention dispatch (the user-ask boundary) is substituted with a
recording async fn — mirroring test_safety_limit_handler / test_budget_limit_unify_1868
which fake only the ask boundary. No mocks of collaborators.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import OnLimitConfig, SafetyConfig
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetCheck
from reyn.runtime.limits.limit_handler import reset_run_extensions
from reyn.runtime.session import Session
from reyn.skill.skill_runner import SkillRunner
from reyn.user_intervention import InterventionAnswer

# ── Site 2: Session._ask_budget_extension honours on_limit.mode ──────────────


def _make_session(tmp_path: Path, *, mode: str, auto_extend_times: int = 1) -> Session:
    safety = SafetyConfig(
        on_limit=OnLimitConfig(
            mode=mode, auto_extend_times=auto_extend_times, ask_timeout_seconds=0,
        ),
    )
    return Session(
        agent_name="alpha",
        safety=safety,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


def _refusal_check() -> BudgetCheck:
    return BudgetCheck(
        allowed=False,
        hard_dimension="per_chain_skill_calls",
        detail="skill 's' hit chain hard-limit",
        context={
            "skill": "s", "chain_id": "c1",
            "current": 2, "hard": 1, "base_hard": 1,
            "extensions_granted": 0, "extension_calls": 3,
        },
    )


def _install_recording_dispatch(session: Session, *, answer_choice: str | None):
    """Replace the intervention dispatch with a recorder; returns the call log."""
    calls: list = []

    async def _dispatch(iv):
        calls.append(iv)
        return InterventionAnswer(text="", choice_id=answer_choice)

    session._dispatch_intervention = _dispatch  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_unattended_denies_without_prompting(tmp_path):
    """Tier 2: on_limit=unattended → deny + NO prompt (the over-prompt bugfix).

    The pre-#1877 hardcoded ``mode="interactive"`` would dispatch an
    intervention here even under unattended (CI/cron). Falsify: a dispatch
    call would mean the bug is back.
    """
    session = _make_session(tmp_path, mode="unattended")
    calls = _install_recording_dispatch(session, answer_choice="yes")

    allowed = await session._ask_budget_extension(
        chain_id="c1", skill_name="s", check=_refusal_check(),
    )

    assert allowed is False
    assert calls == [], "unattended mode must NOT prompt the user"


@pytest.mark.asyncio
async def test_interactive_prompts_and_approves(tmp_path):
    """Tier 2: on_limit=interactive + 'yes' → allow + a prompt was dispatched."""
    session = _make_session(tmp_path, mode="interactive")
    calls = _install_recording_dispatch(session, answer_choice="yes")

    allowed = await session._ask_budget_extension(
        chain_id="c1", skill_name="s", check=_refusal_check(),
    )

    assert allowed is True
    assert len(calls) == 1, "interactive mode must dispatch exactly one prompt"


@pytest.mark.asyncio
async def test_interactive_prompts_and_refuses(tmp_path):
    """Tier 2: on_limit=interactive + 'no' → deny, but a prompt WAS dispatched."""
    session = _make_session(tmp_path, mode="interactive")
    calls = _install_recording_dispatch(session, answer_choice="no")

    allowed = await session._ask_budget_extension(
        chain_id="c1", skill_name="s", check=_refusal_check(),
    )

    assert allowed is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_auto_extend_grants_without_prompting(tmp_path):
    """Tier 2: on_limit=auto_extend → allow (bounded) + NO prompt."""
    reset_run_extensions("c1")
    session = _make_session(tmp_path, mode="auto_extend", auto_extend_times=1)
    calls = _install_recording_dispatch(session, answer_choice="no")

    allowed = await session._ask_budget_extension(
        chain_id="c1", skill_name="s", check=_refusal_check(),
    )

    assert allowed is True, "auto_extend must grant within auto_extend_times"
    assert calls == [], "auto_extend must NOT prompt the user"
    reset_run_extensions("c1")


# ── Site 1: SkillRunner gate keys on extension_calls > 0 (not ask_on_exceed) ──


class _RefusingBudget:
    """check_pre_spawn refuses with a configurable extension_calls in context."""

    def __init__(self, extension_calls: int) -> None:
        self._ext = extension_calls

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> BudgetCheck:
        return BudgetCheck(
            allowed=False,
            hard_dimension="per_chain_skill_calls",
            detail="cap hit",
            context={
                "skill": skill, "chain_id": chain_id,
                "current": 1, "hard": 1, "base_hard": 1,
                "extensions_granted": 0, "extension_calls": self._ext,
            },
        )

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        pass

    def extend_chain_calls(self, *, chain_id: str, skill: str, additional: int) -> int:
        return additional


def _make_gate_runner(*, extension_calls: int) -> tuple[SkillRunner, list]:
    """SkillRunner wired with a refusing budget + a recording ask callback."""
    events = EventLog()
    outbox: asyncio.Queue = asyncio.Queue()
    ask_calls: list = []

    async def _ask_budget_extension(**kwargs) -> bool:
        ask_calls.append(kwargs)
        return False  # decline → caller falls through to the refusal

    async def _put_outbox(msg) -> None:
        await outbox.put(msg)

    async def _enqueue_completed(**kwargs) -> None:
        pass

    runner = SkillRunner(
        event_log=events,
        agent_name="test_agent",
        output_language=None,
        mcp_servers=None,
        allowed_skills=None,
        budget=_RefusingBudget(extension_calls),
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


@pytest.mark.asyncio
async def test_gate_routes_to_ask_when_extension_calls_positive():
    """Tier 2: exceed + extension_calls>0 → SkillRunner calls ask_budget_extension."""
    runner, ask_calls = _make_gate_runner(extension_calls=3)
    await runner.spawn({"skill": "s", "input": {}}, chain_id="c1")
    assert len(ask_calls) == 1, "extension_calls>0 must route the exceed to the ask flow"


@pytest.mark.asyncio
async def test_gate_hard_refuses_when_extension_calls_zero():
    """Tier 2: exceed + extension_calls==0 → hard refuse, ask NOT called (falsify gate)."""
    runner, ask_calls = _make_gate_runner(extension_calls=0)
    await runner.spawn({"skill": "s", "input": {}}, chain_id="c1")
    assert ask_calls == [], "extension_calls==0 must short-circuit to a hard refusal"
