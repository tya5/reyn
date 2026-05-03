"""Tier 2: AgentRegistry + ChatSession invariants for intervention restore.

PR-intervention-link L4+L5. After a process crash, the WAL replay
populates ``AgentSnapshot.outstanding_interventions``. AgentRegistry must
include this in the "non-empty restore" decision and ChatSession must
re-enqueue the restored interventions into the InterventionRegistry so
that:

  - the user can see them via ``/list`` after restart
  - the user can resolve them via ``/answer``
  - the resolution emits ``intervention_resolved`` to the WAL so the
    snapshot's outstanding entry is pruned

This layer does NOT yet route the answer back to the skill (the skill
is not running). That's L6 (SkillRegistry skill-resume awareness).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    """Build a ChatSession redirected to ``tmp_path`` via public kwargs."""
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _snapshot_with_intervention(
    *,
    agent_name: str,
    iv_id: str,
    prompt: str = "Q?",
    run_id: str | None = "rA",
) -> AgentSnapshot:
    snap = AgentSnapshot.empty(agent_name)
    snap.outstanding_interventions[iv_id] = {
        "kind": "ask_user",
        "prompt": prompt,
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
    }
    snap.applied_seq = 5
    return snap


# ---------------------------------------------------------------------------
# L4: AgentRegistry.restore_all condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_restore_all_includes_outstanding_interventions(tmp_path, monkeypatch):
    """Tier 2: a snapshot with only outstanding_interventions triggers session restore.

    Before this fix, an agent with empty inbox + empty pending_chains but
    non-empty outstanding_interventions was skipped during restore_all,
    leaving the user unable to clear the queued intervention after restart.
    """
    monkeypatch.chdir(tmp_path)

    from reyn.chat.registry import AgentRegistry
    from reyn.chat.profile import AgentProfile

    agents_dir = tmp_path / ".reyn" / "agents"
    agents_dir.mkdir(parents=True)
    agent_dir = agents_dir / "alpha"
    state_dir = agent_dir / "state"
    state_dir.mkdir(parents=True)
    AgentProfile.new("alpha", role="").save(agent_dir)

    # Pre-write a snapshot with a stranded intervention (no inbox, no chains)
    snap = _snapshot_with_intervention(agent_name="alpha", iv_id="iv_stranded")
    snap.save(state_dir / "snapshot.json")

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    # session_factory: minimal ChatSession with WAL persistence enabled,
    # consistent with how the chat REPL builds them in production.
    def _factory(profile: AgentProfile):
        s = ChatSession(agent_name=profile.name, state_log=state_log)
        return s
    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=state_log,
    )

    snapshots = await registry.restore_all()

    assert "alpha" in snapshots
    # The session should have been instantiated so the user can interact
    # with the restored interventions. We verify by getting the session
    # and checking its registry has the iv re-enqueued (L5).
    session = registry.get_or_load("alpha")
    # Yield so the restore tasks register the iv into the queue
    for _ in range(3):
        await asyncio.sleep(0)
    iv_ids = [iv.id for iv in session._interventions.list_active()]
    assert "iv_stranded" in iv_ids, (
        f"restored intervention must be in the session's queue; got {iv_ids}"
    )


# ---------------------------------------------------------------------------
# L5: ChatSession.restore_state intervention re-enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_restore_state_re_enqueues_intervention(tmp_path, monkeypatch):
    """Tier 2: outstanding_interventions in snapshot → registry queue populated."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = _snapshot_with_intervention(
        agent_name="alpha", iv_id="iv_recovered", prompt="Recovered Q?",
    )
    session.restore_state(snap)
    # Yield so any restore-time asyncio tasks schedule
    await asyncio.sleep(0)

    actives = session._interventions.list_active()
    assert len(actives) == 1
    assert actives[0].id == "iv_recovered"
    assert actives[0].prompt == "Recovered Q?"
    assert actives[0].run_id == "rA"


@pytest.mark.asyncio
async def test_restored_intervention_can_be_answered(tmp_path, monkeypatch):
    """Tier 2: restored intervention resolves via ``/answer`` (snapshot is pruned).

    End-to-end check that the L3 dispatch wiring + L5 restore re-enqueue
    cooperate: an intervention recovered from the snapshot can be answered
    via the same code path as a freshly-dispatched one, and the WAL gains
    an ``intervention_resolved`` event that prunes the outstanding entry.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = _snapshot_with_intervention(
        agent_name="alpha", iv_id="iv_to_answer", prompt="Answer me",
    )
    session.restore_state(snap)
    # Let any restore tasks start
    for _ in range(3):
        await asyncio.sleep(0)

    consumed = await session._maybe_answer_oldest_intervention("Bob")
    assert consumed is True
    # Yield so the dispatch coroutine's finally clause fires
    for _ in range(3):
        await asyncio.sleep(0)

    # WAL has the resolve event
    log = StateLog(tmp_path / "state.wal")
    events = [e for e in log.iter_from(0) if e["kind"] == "intervention_resolved"]
    assert any(e["intervention_id"] == "iv_to_answer" for e in events)

    # Snapshot pruned (path was the one passed to _make_session)
    snap_path = tmp_path / "alpha_snapshot.json"
    raw = json.loads(snap_path.read_text())
    assert "iv_to_answer" not in raw.get("outstanding_interventions", {})


@pytest.mark.asyncio
async def test_multiple_restored_interventions_preserve_order(tmp_path, monkeypatch):
    """Tier 2: FIFO order of restored interventions matches snapshot order.

    Important UX: the user sees the same announcement order they would
    have seen had no crash occurred.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = AgentSnapshot.empty("alpha")
    for i in range(3):
        iv_id = f"iv_{i}"
        snap.outstanding_interventions[iv_id] = {
            "kind": "ask_user",
            "prompt": f"Q{i}",
            "detail": "",
            "choices": [],
            "suggestions": [],
            "run_id": "rA",
            "skill_name": "demo",
            "id": iv_id,
        }
    snap.applied_seq = 10

    session.restore_state(snap)
    for _ in range(3):
        await asyncio.sleep(0)

    actives = session._interventions.list_active()
    assert [iv.id for iv in actives] == ["iv_0", "iv_1", "iv_2"]


@pytest.mark.asyncio
async def test_restore_state_with_no_interventions_is_noop(tmp_path, monkeypatch):
    """Tier 2: backward compat — empty outstanding_interventions doesn't break."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 0
    session.restore_state(snap)
    for _ in range(3):
        await asyncio.sleep(0)

    assert session._interventions.list_active() == []
