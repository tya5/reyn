"""Tier 2: ``_plan_completer`` surfaces active plan IDs for the TUI picker.

When the user types ``/plan discard `` or ``/plan resume `` the
``InputBar._run_completer`` path queries ``cmd.completer(session, arg_partial)``
and renders the returned strings as hint completions. The previous
state (= pre-#L1) registered ``/plan`` without a completer, so users
had to copy plan_ids by hand from ``/plan list`` output and retype them.

This file pins the contract that the completer:
  - returns active plan IDs after ``discard `` / ``resume ``
  - returns empty otherwise (= ``list`` subcommand, no args, missing session)
  - tolerates a session that doesn't expose ``running_plans`` (= test stubs)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.slash.plan import _plan_completer


class _FakeSession:
    """Minimal session stub exposing only ``running_plans``."""

    def __init__(self, plan_ids: list[str]) -> None:
        # Real session uses ``dict[plan_id -> asyncio.Task]``; the completer
        # only reads ``.keys()`` so values are irrelevant.
        self.running_plans = {pid: object() for pid in plan_ids}


@pytest.mark.asyncio
async def test_plan_completer_returns_plan_ids_after_discard():
    """Tier 2: completer surfaces plan IDs after ``discard `` subcommand."""
    session = _FakeSession(["plan_abc123", "plan_xyz789"])
    result = _plan_completer(session, "discard ")
    assert set(result) == {"plan_abc123", "plan_xyz789"}


@pytest.mark.asyncio
async def test_plan_completer_returns_plan_ids_after_resume():
    """Tier 2: completer surfaces plan IDs after ``resume `` subcommand."""
    session = _FakeSession(["plan_abc123"])
    result = _plan_completer(session, "resume ")
    assert result == ["plan_abc123"]


@pytest.mark.asyncio
async def test_plan_completer_returns_empty_for_list_subcommand():
    """Tier 2: ``list`` takes no plan_id arg; completer returns []."""
    session = _FakeSession(["plan_abc123"])
    assert _plan_completer(session, "list") == []


@pytest.mark.asyncio
async def test_plan_completer_returns_empty_when_no_subcommand_typed():
    """Tier 2: empty arg_partial → empty list (= hint mode is what the user
    needs, not a list of plan IDs they can't act on without a verb)."""
    session = _FakeSession(["plan_abc123"])
    assert _plan_completer(session, "") == []


@pytest.mark.asyncio
async def test_plan_completer_tolerates_session_without_running_plans():
    """Tier 2: stub sessions / unattached state must not crash the completer."""
    class _SessionWithoutRunningPlans:
        pass

    assert _plan_completer(_SessionWithoutRunningPlans(), "discard ") == []


@pytest.mark.asyncio
async def test_plan_completer_step_ids_empty_when_plan_unknown():
    """Tier 2: missing decomposition → empty list (= falls back to hint mode).

    The ``resume <plan_id> --from`` branch loads the decomposition
    artifact for the named plan. When that file doesn't exist (= stale
    plan_id, fresh session, agent without state) the completer must
    return ``[]`` so the picker falls back to plain hint mode rather
    than raising.
    """
    class _Session:
        agent_name = "nonexistent_agent"
        running_plans: dict = {}

    result = _plan_completer(_Session(), "resume plan_does_not_exist --from ")
    assert result == []


