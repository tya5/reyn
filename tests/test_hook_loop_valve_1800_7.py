"""Tier 2: #1800 slice 7 — the hook-driven-turn loop valve.

An E (wake=true) hook fires at turn_end → a new turn → which can fire another …
The valve bounds that chain at the single seam (the top of run_one_iteration,
before any per-turn work): each hook-originated (kind="hook") turn increments a
counter; a human user turn resets it; over the cap the over-limit hook turn is
suppressed after the on_limit checkpoint declines — the session stays alive/idle.

Policy (docs/deep-dives/contributing/testing.md):
- Real Session / EventLog / StateLog / SafetyConfig. Only the LLM boundary
  (_loop_driver.run_turn) is replaced with a plain async recorder. No MagicMock.
- The valve is driven by manual kind="hook" inbox triggers (isolating it from the
  dispatcher); no hooks are configured, so dispatch() at turn_end is a no-op and
  never injects extra triggers.
- on_limit=unattended → the checkpoint denies deterministically (no bus). Events
  observed via the public EventLog subscriber; no private-state assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session


def _make_session(tmp_path: Path, *, cap: int) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(max_hook_driven_turns=cap),
        on_limit=OnLimitConfig(mode="unattended"),   # deny deterministically, no bus
    )
    return Session(
        agent_name="valve-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=safety,
    )


def _fake_run_turn(session: Session) -> list[str]:
    """Replace the LLM boundary with a recorder of the per-turn user_text. The
    recorded texts are the observable proof of which turns actually ran."""
    ran: list[str] = []

    async def _noop(user_text: str, chain_id: str) -> None:
        ran.append(user_text)

    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]
    return ran


def _collect_events(session: Session) -> list[dict]:
    collected: list[dict] = []

    def _sub(event) -> None:  # Event → flat dict (the house-style accessor)
        collected.append({"type": event.type, **event.data})

    session._chat_events.add_subscriber(_sub)
    return collected


def _checkpoint_kinds(events: list[dict]) -> list:
    return [e.get("kind") for e in events if e["type"] == "safety_limit_checkpoint"]


async def _push_hook(session: Session, text: str, *, wake: bool = True) -> None:
    await session._put_inbox("hook", {"name": "turn_end", "text": text, "wake": wake})


async def _push_user(session: Session, text: str) -> None:
    await session._put_inbox("user", {"text": text, "wake": True, "chain_id": "c"})


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_hooks_valve_never_engages(tmp_path):
    """Tier 2: with only human user turns (no hook triggers), the valve never
    engages — every turn runs and no safety checkpoint fires (no-op equivalence
    for the hooks-free path)."""
    session = _make_session(tmp_path, cap=2)
    ran = _fake_run_turn(session)
    events = _collect_events(session)

    await _push_user(session, "u1")
    await _push_user(session, "u2")
    await session.run_one_iteration()
    await session.run_one_iteration()

    assert ran == ["u1", "u2"]                     # both user turns ran
    assert _checkpoint_kinds(events) == []         # valve never tripped


@pytest.mark.asyncio
async def test_hook_loop_exceeding_cap_is_suppressed(tmp_path):
    """Tier 2: a hook chain exceeding the cap trips the checkpoint and the
    over-limit hook turn is SUPPRESSED (does not run) — the chain stops, finite."""
    session = _make_session(tmp_path, cap=2)
    ran = _fake_run_turn(session)
    events = _collect_events(session)

    for text in ("h1", "h2", "h3"):
        await _push_hook(session, text)
    for _ in range(3):
        await session.run_one_iteration()

    # h1, h2 ran (count 1, 2 ≤ cap); h3 (count 3 > cap) suppressed.
    assert ran == ["h1", "h2"]
    assert "hook_driven_turns" in _checkpoint_kinds(events)   # valve trip evented


@pytest.mark.asyncio
async def test_counter_resets_on_user_turn(tmp_path):
    """Tier 2: a human user turn re-arms the budget — a hook that would exceed the
    cap runs after an intervening user turn resets the counter."""
    session = _make_session(tmp_path, cap=1)
    ran = _fake_run_turn(session)

    await _push_hook(session, "h1")     # count 1 ≤ 1 → runs
    await _push_user(session, "u1")     # resets the counter to 0
    await _push_hook(session, "h2")     # count 1 again (NOT 2) → runs
    for _ in range(3):
        await session.run_one_iteration()

    # without the reset, h2 would be count 2 > 1 → suppressed. Its presence proves
    # the user turn re-armed the budget.
    assert ran == ["h1", "u1", "h2"]


@pytest.mark.asyncio
async def test_c_ride_alongs_do_not_increment(tmp_path):
    """Tier 2: a wake=false ride-along (C) drained alongside a trigger does NOT
    count toward the valve — only the kind="hook" trigger does. With cap=1, a C
    riding with the first hook leaves the first hook running (count 1, not 2)."""
    session = _make_session(tmp_path, cap=1)
    ran = _fake_run_turn(session)

    await _push_hook(session, "c0", wake=False)   # a C ride-along (wake=false)
    await _push_hook(session, "h1", wake=True)    # the trigger → count 1 ≤ 1 → runs
    await _push_hook(session, "h2", wake=True)    # count 2 > 1 → suppressed
    for _ in range(2):
        await session.run_one_iteration()

    # h1 ran ⇒ the wake=false C did NOT increment (else count would be 2 → h1
    # suppressed). h2 suppressed by the cap.
    assert ran == ["h1"]
