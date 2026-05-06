"""Tier 2: PR-resume-ux U1 — discard action runtime.

`SkillRegistry.complete(status="discarded")` is the discard path landed in
PR-resume-ux β. It mirrors the normal completion path (WAL append + per-skill
snapshot file unlink) but emits ``skill_discarded`` instead of
``skill_completed``. AgentSnapshot.apply_events handles the new kind so
``active_skill_run_ids`` is pruned on resume replay.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry


def _registry(tmp_path: Path) -> tuple[SkillRegistry, StateLog, Path]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True)
    sl = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=sl,
    )
    return reg, sl, state_dir


def _start_skill(reg: SkillRegistry, run_id: str = "run_disc") -> None:
    asyncio.run(reg.start(
        run_id=run_id, skill_name="demo",
        skill_input={"type": "input", "data": {}},
    ))


def test_complete_with_status_discarded_emits_skill_discarded(tmp_path):
    """Tier 2: status='discarded' → WAL ``skill_discarded`` event (not ``skill_completed``)."""
    reg, sl, _ = _registry(tmp_path)
    _start_skill(reg)
    asyncio.run(reg.complete(run_id="run_disc", status="discarded"))

    events = list(sl.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    completed = [e for e in events if e["kind"] == "skill_completed"]

    assert len(discarded) == 1, (
        f"expected 1 skill_discarded; got {[e['kind'] for e in events]}"
    )
    assert completed == [], (
        "discarded path must NOT emit skill_completed"
    )
    ev = discarded[0]
    assert ev["run_id"] == "run_disc"
    assert ev["target"] == "alpha"


def test_complete_default_status_still_emits_skill_completed(tmp_path):
    """Tier 2: backward compat — no status param → skill_completed (existing behavior)."""
    reg, sl, _ = _registry(tmp_path)
    _start_skill(reg, run_id="run_normal")
    asyncio.run(reg.complete(run_id="run_normal"))

    events = list(sl.iter_from(0))
    completed = [e for e in events if e["kind"] == "skill_completed"]
    assert len(completed) == 1


def test_complete_with_status_discarded_deletes_snapshot_file(tmp_path):
    """Tier 2: discard path also unlinks the per-skill snapshot (same cleanup as completed)."""
    reg, _, state_dir = _registry(tmp_path)
    _start_skill(reg, run_id="run_to_unlink")
    snap_path = state_dir / "skills" / "run_to_unlink.snapshot.json"
    assert snap_path.is_file(), "start must have written the snapshot"

    asyncio.run(reg.complete(run_id="run_to_unlink", status="discarded"))

    assert not snap_path.exists(), "discard must remove the per-skill snapshot file"


def test_agent_snapshot_apply_skill_discarded_prunes_active(tmp_path):
    """Tier 2: AgentSnapshot.apply_events handles skill_discarded → run_id pruned.

    Mirrors the existing skill_completed handler. Without this, restored
    AgentSnapshot would still list discarded run_ids in
    active_skill_run_ids and the next restore_all would try to resume them.
    """
    snap = AgentSnapshot.empty("alpha")
    snap.active_skill_run_ids = ["run_a", "run_b"]
    snap.applied_seq = 0

    snap.apply_events([
        {
            "seq": 1, "kind": "skill_discarded",
            "target": "alpha", "agent": "alpha",
            "run_id": "run_a",
        },
    ])

    assert snap.active_skill_run_ids == ["run_b"]
    assert snap.applied_seq == 1


def test_skill_discarded_in_wal_event_kinds():
    """Tier 2: ``skill_discarded`` is a registered WAL event kind.

    Without registration, ``StateLog.append`` would reject the event
    at write time (validation by WAL_EVENT_KINDS).
    """
    from reyn.events.state_log import WAL_EVENT_KINDS
    assert "skill_discarded" in WAL_EVENT_KINDS


def test_complete_status_invalid_raises(tmp_path):
    """Tier 2: invalid status (e.g. typo) raises early to catch bugs.

    Pinned because the status param is new and a typo like "discard" vs
    "discarded" silently doing the wrong thing would corrupt the WAL.
    """
    reg, _, _ = _registry(tmp_path)
    _start_skill(reg, run_id="run_typo")
    with pytest.raises(ValueError, match="status"):
        asyncio.run(reg.complete(run_id="run_typo", status="discard"))  # typo
