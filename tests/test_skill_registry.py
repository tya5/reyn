"""Tier 2: OS invariant — SkillRegistry coordinates per-skill snapshots and WAL events.

Lifecycle invariants:
  - start() appends skill_started + writes per-skill snapshot
  - advance_phase() appends skill_phase_advanced + updates snapshot fields
  - complete() appends skill_completed + removes snapshot file
  - load_active() repopulates from disk (process restart simulation)

Observation flows through:
  - the WAL file (StateLog.iter_from)
  - the per-skill snapshot file on disk
  - the registry's public read methods (get / list_active)
No mocks — real StateLog, real filesystem under tmp_path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry


def _make_registry(tmp_path: Path) -> tuple[SkillRegistry, StateLog]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )
    return reg, log


def _wal_kinds(log: StateLog) -> list[str]:
    return [e["kind"] for e in log.iter_from(0)]


def _entries(log: StateLog, kind: str) -> list[dict]:
    return [e for e in log.iter_from(0) if e.get("kind") == kind]


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


def test_start_appends_skill_started_and_creates_snapshot(tmp_path):
    """Tier 2: start() emits skill_started WAL event + writes snapshot file.

    The snapshot's ``applied_seq`` and ``last_phase_applied_seq`` are
    stamped to the freshly-allocated WAL seq so the run doesn't pin
    truncation at seq=0.
    """
    reg, log = _make_registry(tmp_path)

    async def go():
        return await reg.start(
            run_id="run_a", skill_name="demo", skill_input={"x": 1},
        )

    snap = asyncio.run(go())
    assert snap.skill_run_id == "run_a"
    assert snap.skill_name == "demo"
    assert snap.applied_seq > 0
    assert snap.last_phase_applied_seq == snap.applied_seq

    # WAL has the event with the agent target field set
    started = _entries(log, "skill_started")[0]
    assert started["target"] == "alpha"
    assert started["agent"] == "alpha"
    assert started["run_id"] == "run_a"
    assert started["skill_name"] == "demo"
    assert started["skill_input"] == {"x": 1}

    # Snapshot file persisted on disk
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "skills" / "run_a.snapshot.json"
    assert snap_path.is_file()


def test_start_caches_snapshot_in_memory(tmp_path):
    """Tier 2: started snapshot is immediately accessible via get() / list_active()."""
    reg, _ = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="r1", skill_name="s", skill_input={})
        await reg.start(run_id="r2", skill_name="s", skill_input={})

    asyncio.run(go())
    assert sorted(reg.list_active()) == ["r1", "r2"]
    assert reg.get("r1") is not None
    assert reg.get("nonexistent") is None


# ---------------------------------------------------------------------------
# advance_phase()
# ---------------------------------------------------------------------------


def test_advance_phase_updates_snapshot_and_appends_event(tmp_path):
    """Tier 2: advance_phase emits skill_phase_advanced + bumps last_phase_applied_seq, current_phase, history, visit_counts."""
    reg, log = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        await reg.advance_phase(
            run_id="r", next_phase="draft",
            last_phase_artifact_path="ws/v1.json",
        )
        await reg.advance_phase(
            run_id="r", next_phase="review",
            last_phase_artifact_path="ws/v2.json",
        )
        return reg.get("r")

    snap = asyncio.run(go())
    assert snap.current_phase == "review"
    assert snap.history == ["draft", "review"]
    assert snap.visit_counts == {"draft": 1, "review": 1}
    assert snap.last_phase_artifact_path == "ws/v2.json"
    assert snap.last_phase_applied_seq > 0

    advanced = _entries(log, "skill_phase_advanced")
    adv0, adv1 = advanced
    assert adv0["next_phase"] == "draft"
    assert adv1["next_phase"] == "review"


def test_advance_phase_unknown_run_id_is_noop(tmp_path):
    """Tier 2: advancing a never-registered run_id does not append a WAL event or crash."""
    reg, log = _make_registry(tmp_path)

    async def go():
        await reg.advance_phase(run_id="ghost", next_phase="draft")

    asyncio.run(go())
    assert _wal_kinds(log) == []


def test_advance_phase_increments_visit_counts(tmp_path):
    """Tier 2: re-entering a phase bumps its visit count (resume needs this for cycle detection)."""
    reg, _ = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        for _ in range(3):
            await reg.advance_phase(run_id="r", next_phase="loop")
        return reg.get("r")

    snap = asyncio.run(go())
    assert snap.visit_counts == {"loop": 3}
    assert snap.history == ["loop", "loop", "loop"]


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


def test_complete_appends_event_and_removes_snapshot(tmp_path):
    """Tier 2: complete() emits skill_completed + deletes snapshot file + drops in-memory entry."""
    reg, log = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        await reg.complete(run_id="r")

    asyncio.run(go())
    assert _wal_kinds(log) == ["skill_started", "skill_completed"]
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "skills" / "r.snapshot.json"
    assert not snap_path.exists()
    assert reg.get("r") is None
    assert reg.list_active() == []


def test_complete_unknown_run_id_still_appends_event(tmp_path):
    """Tier 2: completing an unknown run_id is safe (idempotent / replay-friendly).

    Resume scenario: after restart, the registry hasn't seen this run yet
    (load_active() finds the snapshot, then completion lands).
    """
    reg, log = _make_registry(tmp_path)

    async def go():
        await reg.complete(run_id="ghost")

    asyncio.run(go())
    assert "skill_completed" in _wal_kinds(log)


# ---------------------------------------------------------------------------
# load_active() — restart simulation
# ---------------------------------------------------------------------------


def test_load_active_repopulates_cache_from_disk(tmp_path):
    """Tier 2: load_active() rehydrates the in-memory cache from on-disk snapshots."""
    reg, _ = _make_registry(tmp_path)

    async def setup():
        await reg.start(run_id="r1", skill_name="s", skill_input={"x": 1})
        await reg.start(run_id="r2", skill_name="s", skill_input={"x": 2})
        await reg.advance_phase(run_id="r1", next_phase="draft")

    asyncio.run(setup())

    # Simulate process restart with a fresh registry over the same state dir
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg2 = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log2,
    )
    loaded = reg2.load_active()
    assert sorted(loaded.keys()) == ["r1", "r2"]
    assert reg2.get("r1").current_phase == "draft"
    assert reg2.get("r2").current_phase == ""


def test_load_active_skips_corrupt_snapshot(tmp_path):
    """Tier 2: a corrupt snapshot file is skipped with a warning, others load.

    SkillSnapshot.load is defensive (returns empty), so we additionally
    verify load_active doesn't crash on bad files. Note: SkillSnapshot's
    defensive empty fallback means a corrupt file will load as an empty
    snapshot rather than being dropped — the file is logged but still
    appears in the cache. This test pins that behavior so later refactors
    can decide whether to harden it.
    """
    reg, _ = _make_registry(tmp_path)

    async def setup():
        await reg.start(run_id="good", skill_name="s", skill_input={})

    asyncio.run(setup())

    skills_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "skills"
    (skills_dir / "bad.snapshot.json").write_text("{not json", encoding="utf-8")

    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg2 = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log2,
    )
    loaded = reg2.load_active()
    # Both files are loaded; the corrupt one returns a defensive empty
    # snapshot via SkillSnapshot.load. Behavior is observable here so a
    # later hardening can deliberately change it.
    assert "good" in loaded
    assert "bad" in loaded
    assert loaded["bad"].applied_seq == 0


def test_load_active_no_skills_dir_returns_empty(tmp_path):
    """Tier 2: load_active() handles a missing skills directory cleanly."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )
    assert reg.load_active() == {}
    assert reg.list_active() == []


# ---------------------------------------------------------------------------
# state_log=None mode (tests / non-chat invocation)
# ---------------------------------------------------------------------------


def test_truncate_hook_fires_after_phase_advance(tmp_path):
    """Tier 2: ``advance_phase`` invokes the truncate-eligible hook after the WAL append."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    fired: list[str] = []

    async def hook():
        fired.append("called")

    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
        truncate_eligible_hook=hook,
    )

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        # start() does NOT fire the hook (skill_started isn't a truncation
        # trigger — it makes the WAL longer, not shorter).
        assert fired == []
        await reg.advance_phase(run_id="r", next_phase="draft")
        await reg.advance_phase(run_id="r", next_phase="review")

    asyncio.run(go())
    assert fired == ["called", "called"]


def test_truncate_hook_fires_after_skill_completed(tmp_path):
    """Tier 2: ``complete`` invokes the truncate-eligible hook after the WAL append."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    fired: list[str] = []

    async def hook():
        fired.append("called")

    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
        truncate_eligible_hook=hook,
    )

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        await reg.complete(run_id="r")

    asyncio.run(go())
    assert fired == ["called"]


def test_truncate_hook_exception_does_not_propagate(tmp_path):
    """Tier 2: a raising hook is logged + swallowed; advance_phase still succeeds."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    async def hook():
        raise RuntimeError("bad hook")

    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
        truncate_eligible_hook=hook,
    )

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        # Must not raise even though hook does.
        await reg.advance_phase(run_id="r", next_phase="draft")
        return reg.get("r")

    snap = asyncio.run(go())
    assert snap.current_phase == "draft"  # advance_phase still completed


# ---------------------------------------------------------------------------
# mark_awaiting / clear_awaiting (R-D16)
# ---------------------------------------------------------------------------


def test_mark_awaiting_stamps_snapshot(tmp_path):
    """Tier 2: mark_awaiting() sets ``awaiting_since`` (monotonic) and
    ``awaiting_intervention_id`` on the per-skill snapshot."""
    reg, _log = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="run_a", skill_name="s", skill_input={})
        reg.mark_awaiting(run_id="run_a", intervention_id="iv-1")
        return reg.get("run_a")

    snap = asyncio.run(go())
    assert snap.awaiting_since is not None
    assert isinstance(snap.awaiting_since, float)
    assert snap.awaiting_intervention_id == "iv-1"


def test_clear_awaiting_resets_snapshot(tmp_path):
    """Tier 2: clear_awaiting() restores both fields to None."""
    reg, _log = _make_registry(tmp_path)

    async def go():
        await reg.start(run_id="run_b", skill_name="s", skill_input={})
        reg.mark_awaiting(run_id="run_b", intervention_id="iv-2")
        reg.clear_awaiting(run_id="run_b")
        return reg.get("run_b")

    snap = asyncio.run(go())
    assert snap.awaiting_since is None
    assert snap.awaiting_intervention_id is None


def test_mark_clear_awaiting_unknown_run_id_is_noop(tmp_path):
    """Tier 2: marking/clearing a non-tracked run is a no-op (defensive)."""
    reg, _log = _make_registry(tmp_path)

    # Should not raise
    reg.mark_awaiting(run_id="ghost", intervention_id="iv")
    reg.clear_awaiting(run_id="ghost")
    assert reg.get("ghost") is None


def test_lifecycle_works_without_state_log(tmp_path):
    """Tier 2: start/advance/complete are all no-ops on the WAL when state_log is None, but the in-memory cache and snapshot file still update.

    Mirrors the convention from AgentRegistry / SnapshotJournal — a
    None state_log is the test/standalone mode signal.
    """
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=None,
    )

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        await reg.advance_phase(run_id="r", next_phase="p1")
        await reg.complete(run_id="r")

    asyncio.run(go())  # should not raise
    # WAL doesn't exist; snapshot file removed by complete()
    assert not (state_dir / "skills" / "r.snapshot.json").exists()
