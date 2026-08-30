"""Tier 2: #1800 slice 7 — the hook-driven-turn loop valve.

An E (wake=true) hook fires at turn_end → a new turn → which can fire another …
The valve bounds that chain at the single seam (the top of run_one_iteration,
before any per-turn work): each hook-originated (kind="hook") turn increments a
counter; a human user turn resets it; over the cap the over-limit hook turn is
suppressed after the on_limit checkpoint declines — the session stays alive/idle.

Policy (docs/deep-dives/contributing/testing.md):
- Real Session / EventLog / StateLog / SafetyConfig. #5103 ③ migration: a real
  turn now dispatches for real (@pytest.mark.llm_stub, only litellm.acompletion
  is stubbed) instead of replacing `_loop_driver.run_turn` with a private
  recorder — "which turns actually ran" is now read off the public
  `turn_started` audit-event stream, joined on the chain_id each push chose,
  never off a private closure's own list.
- The valve is driven by manual kind="hook" inbox triggers (isolating it from the
  dispatcher); no hooks are configured, so dispatch() at turn_end is a no-op and
  never injects extra triggers.
- on_limit=unattended → the checkpoint denies deterministically (no bus). Events
  observed via the public Session.subscribe_audit_events seam; no private-state
  assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle


def _make_session(tmp_path: Path, *, cap: int) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(max_hook_driven_turns=cap),
        on_limit=OnLimitConfig(mode="unattended"),   # deny deterministically, no bus
    )
    return make_session(
        agent_name="valve-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=safety,
    )


def _collect_events(session: Session) -> list[dict]:
    collected: list[dict] = []

    def _sub(event) -> None:  # Event → flat dict (the house-style accessor)
        collected.append({"type": event.type, **event.data})

    # #5103 ③: the public seam (#5260) — never session._audit_events directly.
    session.subscribe_audit_events(_sub)
    return collected


def _ran_chain_ids(events: list[dict]) -> list[str]:
    """The chain_ids of every dispatched turn, in dispatch order — the
    observable proof of which pushes actually ran, replacing the private
    `_fake_run_turn`/`ran` recorder this file used before #5103 ③. A push
    that was suppressed by the valve never reaches `_run_turn_body`, so it
    never emits `turn_started` — same absence-is-the-signal shape the old
    recorder had (a suppressed text never landed in `ran` either)."""
    return [e["chain_id"] for e in events if e["type"] == "turn_started"]


def _checkpoint_kinds(events: list[dict]) -> list:
    return [e.get("kind") for e in events if e["type"] == "safety_limit_checkpoint"]


async def _push_hook(
    session: Session, chain_id: str, *, wake: bool = True,
) -> None:
    await session._put_inbox(
        "hook", {"name": "turn_end", "text": chain_id, "wake": wake, "chain_id": chain_id},
    )


async def _push_user(session: Session, chain_id: str) -> None:
    await session._put_inbox(
        "user", {"text": chain_id, "wake": True, "chain_id": chain_id},
    )


@pytest.mark.asyncio
async def test_hook_message_is_fanned_out_to_live_outbox(tmp_path):
    """Tier 2: a hook-injected message is visible on the live outbox, not only history."""
    session = _make_session(tmp_path, cap=2)
    subscription = session.outbox_hub.subscribe()
    async def _noop(*_args):
        return None

    session._run_router_loop = _noop  # type: ignore[method-assign]

    await session._handle_hook_message({"name": "probe", "text": "injected", "wake": True})
    message = await subscription.get()
    assert message is not None
    assert message.kind == "system"
    assert "[hook:probe]" in message.text
    subscription.close()


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_no_hooks_valve_never_engages(tmp_path):
    """Tier 2: with only human user turns (no hook triggers), the valve never
    engages — every turn runs and no safety checkpoint fires (no-op equivalence
    for the hooks-free path)."""
    session = _make_session(tmp_path, cap=2)
    events = _collect_events(session)

    # #5103 ③: pushed one-at-a-time, not all upfront — a REAL turn now
    # dispatches (llm_stub), and its own real mid-turn-injection peek
    # (Session._inbox_arbiter.peek_mid_turn_injection, #3792) would
    # otherwise silently fold an already-queued next message into the
    # turn in flight instead of leaving it for the next
    # run_one_iteration() call. The private `_noop` this file used
    # before #5103 returned instantly with no await inside it, so that
    # peek never got a scheduling window to fire — an accidental
    # coupling between "the LLM boundary is a no-op" and "queued
    # messages never overlap a turn" that a real dispatch breaks.
    await _push_user(session, "u1")
    await session.run_one_iteration()
    await _push_user(session, "u2")
    await session.run_one_iteration()
    await settle(session)

    assert _ran_chain_ids(events) == ["u1", "u2"]  # both user turns ran
    assert _checkpoint_kinds(events) == []         # valve never tripped


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_hook_loop_exceeding_cap_is_suppressed(tmp_path):
    """Tier 2: a hook chain exceeding the cap trips the checkpoint and the
    over-limit hook turn is SUPPRESSED (does not run) — the chain stops, finite."""
    session = _make_session(tmp_path, cap=2)
    events = _collect_events(session)

    # push-then-iterate, not all-upfront — see the mid-turn-injection note
    # in test_no_hooks_valve_never_engages above.
    for chain_id in ("h1", "h2", "h3"):
        await _push_hook(session, chain_id)
        await session.run_one_iteration()
    await settle(session)

    # h1, h2 ran (count 1, 2 ≤ cap); h3 (count 3 > cap) suppressed.
    assert _ran_chain_ids(events) == ["h1", "h2"]
    assert "hook_driven_turns" in _checkpoint_kinds(events)   # valve trip evented


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_counter_resets_on_user_turn(tmp_path):
    """Tier 2: a human user turn re-arms the budget — a hook that would exceed the
    cap runs after an intervening user turn resets the counter."""
    session = _make_session(tmp_path, cap=1)
    events = _collect_events(session)

    # push-then-iterate, not all-upfront — see the mid-turn-injection note
    # in test_no_hooks_valve_never_engages above.
    await _push_hook(session, "h1")     # count 1 ≤ 1 → runs
    await session.run_one_iteration()
    await _push_user(session, "u1")     # resets the counter to 0
    await session.run_one_iteration()
    await _push_hook(session, "h2")     # count 1 again (NOT 2) → runs
    await session.run_one_iteration()
    await settle(session)

    # without the reset, h2 would be count 2 > 1 → suppressed. Its presence proves
    # the user turn re-armed the budget.
    assert _ran_chain_ids(events) == ["h1", "u1", "h2"]


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_c_ride_alongs_do_not_increment(tmp_path):
    """Tier 2: a wake=false ride-along (C) drained alongside a trigger does NOT
    count toward the valve — only the kind="hook" trigger does. With cap=1, a C
    riding with the first hook leaves the first hook running (count 1, not 2)."""
    session = _make_session(tmp_path, cap=1)
    events = _collect_events(session)

    # c0+h1 pushed together and drained together (drain_to_wake bundles a
    # wake=false ride-along with the NEXT wake=true trigger atomically,
    # before dispatch) — safe from the mid-turn-injection hazard the other
    # tests' comment explains. h2 is pushed only AFTER h1's real turn
    # dispatch completes, for the same reason those tests split their pushes.
    await _push_hook(session, "c0", wake=False)   # a C ride-along (wake=false)
    await _push_hook(session, "h1", wake=True)    # the trigger → count 1 ≤ 1 → runs
    await session.run_one_iteration()
    await _push_hook(session, "h2", wake=True)    # count 2 > 1 → suppressed
    await session.run_one_iteration()
    await settle(session)

    # h1 ran ⇒ the wake=false C did NOT increment (else count would be 2 → h1
    # suppressed). h2 suppressed by the cap.
    assert _ran_chain_ids(events) == ["h1"]
