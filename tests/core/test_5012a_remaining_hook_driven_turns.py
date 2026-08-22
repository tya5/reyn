"""Tier 2: `Session.remaining_hook_driven_turns` (#5012-A) — the loop-valve
SSoT witness.

Real Session, no mocks (matches testing.md policy). Two properties under
test: the countdown arithmetic itself, and that `remaining_hook_driven_turns`
reads the SAME cap computation the enforcement site
(`_stamp_execution_context`'s `TurnOrigin.HOOK` branch) uses — via the shared
`_effective_hook_driven_turns_cap` helper — rather than a second, independently
maintained copy of the formula (lead-coder catch, #5012-A review).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path, *, cap: int) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(max_hook_driven_turns=cap),
        on_limit=OnLimitConfig(mode="unattended"),
    )
    return make_session(
        agent_name="valve-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=safety,
    )


def test_remaining_is_the_full_cap_before_any_hook_turn(tmp_path: Path) -> None:
    """Tier 2: no hook-driven turns spent yet → remaining == cap."""
    session = _make_session(tmp_path, cap=5)
    assert session.remaining_hook_driven_turns == 5


async def _push_hook(session: Session, text: str, *, wake: bool = True) -> None:
    await session._put_inbox("hook", {"name": "turn_end", "text": text, "wake": wake})


@pytest.mark.asyncio
async def test_remaining_counts_down_as_hook_turns_are_spent(tmp_path: Path) -> None:
    """Tier 2: each hook-driven turn actually run decrements what
    `remaining_hook_driven_turns` reports — the countdown is real, not a
    static echo of the config value."""
    session = _make_session(tmp_path, cap=3)

    async def _noop(user_text: str, chain_id: str) -> None:
        pass

    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]

    assert session.remaining_hook_driven_turns == 3
    await _push_hook(session, "h1")
    await session.run_one_iteration()
    assert session.remaining_hook_driven_turns == 2
    await _push_hook(session, "h2")
    await session.run_one_iteration()
    assert session.remaining_hook_driven_turns == 1


def test_remaining_is_none_when_uncapped(tmp_path: Path) -> None:
    """Tier 2: `max_hook_driven_turns <= 0` means the valve enforces no cap —
    reporting a number would fabricate a limit that does not exist."""
    session = _make_session(tmp_path, cap=0)
    assert session.remaining_hook_driven_turns is None


def test_remaining_reflects_a_session_local_extension(tmp_path: Path) -> None:
    """Tier 2: SSoT witness, via the public property only — granting a
    session-local extension (the same `_safety_extensions` bucket a real
    checkpoint decision writes into) changes what
    `remaining_hook_driven_turns` reports, proving it reads the LIVE
    effective cap rather than a static copy of the config value taken once
    at construction."""
    session = _make_session(tmp_path, cap=2)
    assert session.remaining_hook_driven_turns == 2

    session._safety_extensions["hook_driven_turns"] = 3.0

    assert session.remaining_hook_driven_turns == 5
