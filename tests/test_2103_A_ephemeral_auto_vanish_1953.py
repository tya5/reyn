"""Tier 2: #2103 A — a spawned EPHEMERAL session auto-vanishes once its task is done.

session-spawn records a spawn-time ``mode`` (ephemeral | persistent). This slice wires
the ephemeral LIFECYCLE: the registry marks an ephemeral spawn, and the session, after a
turn that leaves the inbox drained (its task done, the run-loop about to idle-block),
schedules a DETACHED teardown via the registry ``remove_session`` seam (the SAME teardown
the rewind as-of-cut drop uses) — drops the session, closes its per-session Task backend,
emits ``session_vanished``, purges the dir. A PERSISTENT spawn survives.

Real AgentRegistry + Session (no mocks; the factory passes ``registry`` exactly as the
production scoped_session_factory does, via ``**base``). Assertions are on the PUBLIC
``registry.session_ids`` surface (the observable: vanished vs survived) — not internal
flags. FALSIFY: drop the registry ephemeral mark OR the
``_maybe_schedule_ephemeral_vanish`` trigger → the ephemeral session survives → RED.

Live-path note (close-review / tui): the trigger fires from ``run_one_iteration``'s
turn-end (after the ``finally``). These tests invoke that seam directly (every turn kind
otherwise needs a live LLM); the full run_one_iteration → vanish path is for tui
live-verify (the §16 B1 precedent — automated test simulates the post-turn call, tui
verifies the live run-loop).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry whose factory passes ``registry=reg`` to each Session —
    mirroring the production scoped_session_factory (so a spawned session has the
    ``_registry`` ref its auto-vanish needs)."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log,
                    registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


@pytest.mark.asyncio
async def test_ephemeral_spawn_auto_vanishes_persistent_survives(tmp_path):
    """Tier 2: #2103 A — once its task is done (inbox drained), an ephemeral spawn
    auto-vanishes (gone from the public ``session_ids``); a persistent spawn survives.
    RED if the ephemeral mark or the vanish trigger is dropped (it would survive)."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")  # the live main session

    eph_sid = await reg.spawn_session_recorded("alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None)
    per_sid = await reg.spawn_session_recorded("alice", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    live = reg.session_ids("alice")
    assert eph_sid in live and per_sid in live  # both live initially (public surface)

    # task done (inbox drained) → fire the post-turn ephemeral check on both sessions.
    eph = reg._peek_session("alice", eph_sid)
    per = reg._peek_session("alice", per_sid)
    eph._maybe_schedule_ephemeral_vanish()
    per._maybe_schedule_ephemeral_vanish()
    # await the ephemeral teardown; the persistent session scheduled nothing.
    vanish = eph._vanish_task
    if vanish is not None:
        await vanish

    live_after = reg.session_ids("alice")
    assert eph_sid not in live_after   # vanished
    assert per_sid in live_after        # survived (the ephemeral guard held)


@pytest.mark.asyncio
async def test_ephemeral_does_not_vanish_while_awaiting_delegation(tmp_path):
    """Tier 2: #2103 A — the awaited-work guard. A spawned ephemeral session that has
    DELEGATED and awaits a peer ``agent_response`` (a pending chain) has a
    transiently-empty inbox mid-await; it must NOT vanish (purging its dir + emitting
    session_vanished before the response lands = silent + destructive). A spawned
    session has the full ChainManager + send_to_agent wiring, so this is reachable. RED
    if the awaited-work guard is dropped (it vanishes mid-await)."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None)
    eph = reg._peek_session("alice", sid)

    # the spawned session delegated to a peer + awaits its response (pending chain;
    # the inbox is transiently empty between the delegate-send and the response).
    await eph._chains.register(chain_id="c1", from_user=False, depth=1,
                               original_text="sub", sender="alice", waiting_on={"peer"})
    eph._maybe_schedule_ephemeral_vanish()
    # drain any (erroneously) scheduled teardown so the assertion reflects the true
    # outcome — without this a stripped guard would schedule a DETACHED vanish that
    # hasn't run yet at the assert, hiding the regression.
    if eph._vanish_task is not None:
        await eph._vanish_task

    # the guard held: NOT vanished mid-await (public surface).
    assert sid in reg.session_ids("alice")


@pytest.mark.asyncio
async def test_ephemeral_vanish_scheduled_once(tmp_path):
    """Tier 2: #2103 A — the schedule is idempotent: a multi-turn ephemeral session
    that hits the post-turn check twice tears down ONCE (no double-teardown error), and
    vanishes. RED if the guard is dropped (a second teardown of an already-dropped
    session)."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None)
    eph = reg._peek_session("alice", sid)

    eph._maybe_schedule_ephemeral_vanish()
    eph._maybe_schedule_ephemeral_vanish()  # second post-turn check — must not re-schedule
    vanish = eph._vanish_task
    if vanish is not None:
        await vanish

    assert sid not in reg.session_ids("alice")  # vanished exactly once, cleanly
