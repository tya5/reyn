"""Tier 2: FP-0043 Stage 5 goal-proof — per-session crash-recovery isolation.

The S5 promise: a spawned conversation session's state survives a restart
INDEPENDENTLY of the agent's "main" session. This is the persistence analogue
of S4a's in-memory isolation test — it proves the on-disk substrate (per-session
snapshot path + session_id-routed WAL replay) keeps each session's recovery
state separate.

Mechanism under test (all via the public surface):
  - spawn_session re-keys a spawned session's snapshot to its own per-session
    path (no collision with main's snapshot.json);
  - every WAL append carries the session's session_id (the _wal_append funnel);
  - restore_all discovers main + per-session snapshots, replays the SHARED WAL
    routing each entry by (agent, session_id), and re-adopts each session's state.

State is carried as an outstanding intervention (rather than an inbox message)
so restore re-enqueues it WITHOUT starting a live LLM turn — the same hermetic
pattern as test_intervention_restore — keeping this test deterministic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.core.events.state_log import StateLog


def _make_registry(tmp_path: Path, wal: Path) -> AgentRegistry:
    """Build a registry whose sessions share one WAL (= the production wiring).

    A fresh registry over the SAME project_root + wal file simulates a process
    restart: it re-discovers on-disk snapshots and replays the WAL tail.
    """
    state_log = StateLog(wal)

    def _factory(profile: AgentProfile) -> ChatSession:
        s = ChatSession(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")  # satisfy listener-presence guard
        return s

    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=state_log,
    )


def _iv_dict(iv_id: str, run_id: str) -> dict:
    return {
        "kind": "ask_user",
        "prompt": f"Q-{iv_id}?",
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
    }


def _active_iv_ids(session: "ChatSession") -> list[str]:
    """Public read of a session's restored, re-enqueued interventions."""
    return [iv.id for iv in session.interventions.list_active()]


@pytest.mark.asyncio
async def test_multi_session_restore_is_per_session_independent(tmp_path, monkeypatch):
    """Tier 2: spawn → state → drop + restore_all → main & spawned each restore alone.

    Asserts BOTH directions of isolation: each restored session has exactly its
    own intervention and NOT the other's. A regression where spawned WAL entries
    fell to "main" (the cond-4 forwarding gap) — or where the two sessions shared
    one snapshot.json — would surface as cross-contamination or a missing iv.
    """
    # chdir so a factory-built ChatSession's default snapshot path
    # (.reyn/agents/<name>/state/snapshot.json) resolves under tmp_path, aligning
    # the session's base with registry._dir (the base-alignment invariant S5 relies on).
    monkeypatch.chdir(tmp_path)

    agent_dir = tmp_path / ".reyn" / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    AgentProfile.new("alpha", role="").save(agent_dir)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── pre-crash: open main + a spawned session, assign distinct state ──
    reg1 = _make_registry(tmp_path, wal)
    main1 = reg1.get_or_load("alpha")                 # the implicit "main" session
    sid = reg1.spawn_session("alpha")                 # a second session under the same agent
    spawned1 = reg1.get_session("alpha", sid)
    assert spawned1 is not None

    await main1.journal.record_intervention_dispatched(
        intervention_id="iv_main", iv_dict=_iv_dict("iv_main", "rM"),
    )
    await spawned1.journal.record_intervention_dispatched(
        intervention_id="iv_spawn", iv_dict=_iv_dict("iv_spawn", "rS"),
    )

    # per-session snapshots persist to DISTINCT paths (no collision).
    main_path = agent_dir / "state" / "snapshot.json"
    spawn_path = agent_dir / "state" / "sessions" / sid / "snapshot.json"
    assert main_path.is_file(), "main session snapshot must persist at the legacy path"
    assert spawn_path.is_file(), "spawned session snapshot must persist at its per-session path"

    # ── restart: a fresh registry over the same project_root + WAL ──
    reg2 = _make_registry(tmp_path, wal)
    snapshots = await reg2.restore_all()

    # back-compat return: keyed by agent name = the main session's snapshot.
    assert "alpha" in snapshots
    assert list(snapshots["alpha"].outstanding_interventions) == ["iv_main"]

    # let restore_state's re-enqueue tasks register the interventions.
    for _ in range(3):
        await asyncio.sleep(0)

    # main restored ONLY its own intervention.
    main2 = reg2.get_session("alpha")
    assert main2 is not None
    assert _active_iv_ids(main2) == ["iv_main"]

    # spawned was recreated by restore_all and restored ONLY its own intervention.
    spawned2 = reg2.get_session("alpha", sid)
    assert spawned2 is not None, "restore_all must recreate the spawned session"
    assert _active_iv_ids(spawned2) == ["iv_spawn"]

    # explicit cross-contamination guard (both directions).
    assert "iv_spawn" not in _active_iv_ids(main2)
    assert "iv_main" not in _active_iv_ids(spawned2)
