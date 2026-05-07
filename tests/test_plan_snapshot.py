"""Tier 2: PlanSnapshot persistence + version refuse (ADR-0023 §3.1).

Step 2 of the Phase 2 migration path. Mirrors test_skill_snapshot.py's
shape — round-trip, atomic save, schema-version refuse, default field
defaults.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.events.agent_snapshot import SchemaVersionError
from reyn.plan import (
    PLAN_SNAPSHOT_VERSION,
    PlanSnapshot,
    plan_snapshot_path,
)


def test_empty_factory_initializes_minimum_fields() -> None:
    """Tier 2: ``empty()`` produces a snapshot at plan_started time
    (= no steps observed yet, all derived fields default)."""
    snap = PlanSnapshot.empty(
        plan_id="ab12cd34",
        agent_name="default",
        chain_id="chain001",
        goal="compare A and B",
    )
    assert snap.plan_id == "ab12cd34"
    assert snap.agent_name == "default"
    assert snap.chain_id == "chain001"
    assert snap.goal == "compare A and B"
    assert snap.applied_seq == 0
    assert snap.last_step_applied_seq == 0
    assert snap.decomposition_artifact_path is None
    assert snap.steps_serialized == []
    assert snap.step_results == {}
    assert snap.step_failures == {}
    assert snap.current_step_id is None
    assert snap.last_committed_step_id is None
    assert snap.spawned_skill_run_ids == {}
    assert snap.parent_skill_run_id is None
    assert snap.usage_tokens_so_far is None


def test_save_load_round_trip(tmp_path: Path) -> None:
    """Tier 2: save → load preserves every field."""
    snap = PlanSnapshot(
        plan_id="p001",
        agent_name="default",
        chain_id="c001",
        goal="g",
        applied_seq=42,
        last_step_applied_seq=40,
        decomposition_artifact_path="/abs/path/decomposition.json",
        steps_serialized=[
            {"id": "s1", "description": "first", "tools": ["read_file"], "depends_on": []},
        ],
        step_results={"s1": "result text"},
        step_failures={"s2": "RuntimeError('boom')"},
        current_step_id="s2",
        last_committed_step_id="s1",
        spawned_skill_run_ids={"s2": "child_run_x"},
        parent_skill_run_id=None,
        usage_tokens_so_far={"prompt": 1234, "completion": 567},
    )
    path = plan_snapshot_path(tmp_path, "p001")
    snap.save(path)

    loaded = PlanSnapshot.load("p001", path)
    assert loaded == snap


def test_save_writes_documented_path(tmp_path: Path) -> None:
    """Tier 2: snapshot lives at ``<agent_state>/plans/<plan_id>.snapshot.json``
    (= sibling to the per-plan directory holding the decomposition)."""
    snap = PlanSnapshot.empty(
        plan_id="p001", agent_name="default", chain_id="c001", goal="g"
    )
    path = plan_snapshot_path(tmp_path, "p001")
    snap.save(path)
    assert path == tmp_path / "plans" / "p001.snapshot.json"
    assert path.exists()


def test_save_is_atomic_no_tmp_residue(tmp_path: Path) -> None:
    """Tier 2: save uses tmp + rename; no .tmp file remains after success."""
    snap = PlanSnapshot.empty(
        plan_id="p001", agent_name="default", chain_id="c001", goal="g"
    )
    path = plan_snapshot_path(tmp_path, "p001")
    snap.save(path)
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """Tier 2: missing file yields a usable empty snapshot keyed by
    plan_id (= mirror SkillSnapshot.load resilience)."""
    snap = PlanSnapshot.load("p001", plan_snapshot_path(tmp_path, "p001"))
    assert snap.plan_id == "p001"
    assert snap.applied_seq == 0
    assert snap.goal == ""


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    """Tier 2: malformed JSON falls back to empty (= same posture as
    SkillSnapshot.load — defensive load)."""
    path = plan_snapshot_path(tmp_path, "p001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {", encoding="utf-8")

    snap = PlanSnapshot.load("p001", path)
    assert snap.plan_id == "p001"


def test_load_wrong_schema_version_raises(tmp_path: Path) -> None:
    """Tier 2: schema_version drift raises SchemaVersionError so the
    caller refuses to resume rather than silently load stale fields
    (= ADR-0006 + SkillSnapshot precedent)."""
    path = plan_snapshot_path(tmp_path, "p001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 999, "plan_id": "p001"}),
        encoding="utf-8",
    )

    with pytest.raises(SchemaVersionError, match="version"):
        PlanSnapshot.load("p001", path)


def test_save_emits_documented_schema(tmp_path: Path) -> None:
    """Tier 2: persisted JSON contains schema_version = PLAN_SNAPSHOT_VERSION
    and the documented field set."""
    snap = PlanSnapshot.empty(
        plan_id="p001", agent_name="default", chain_id="c001", goal="g"
    )
    path = plan_snapshot_path(tmp_path, "p001")
    snap.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == PLAN_SNAPSHOT_VERSION
    expected_keys = {
        "schema_version",
        "plan_id",
        "agent_name",
        "chain_id",
        "goal",
        "applied_seq",
        "last_step_applied_seq",
        "decomposition_artifact_path",
        "steps_serialized",
        "step_results",
        "step_failures",
        "current_step_id",
        "last_committed_step_id",
        "spawned_skill_run_ids",
        "parent_skill_run_id",
        "usage_tokens_so_far",
    }
    assert set(payload.keys()) == expected_keys


def test_load_handles_old_file_with_missing_optional_fields(tmp_path: Path) -> None:
    """Tier 2: a file written with the documented ``schema_version`` but
    missing optional fields loads with sensible defaults (= forward compat
    if Phase 2.1 adds new optional fields, old files keep loading)."""
    path = plan_snapshot_path(tmp_path, "p001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": PLAN_SNAPSHOT_VERSION,
                "plan_id": "p001",
                "agent_name": "default",
                "chain_id": "c001",
                "goal": "g",
                "applied_seq": 5,
                "last_step_applied_seq": 4,
            }
        ),
        encoding="utf-8",
    )
    snap = PlanSnapshot.load("p001", path)
    assert snap.plan_id == "p001"
    assert snap.applied_seq == 5
    assert snap.last_step_applied_seq == 4
    # optional fields default
    assert snap.steps_serialized == []
    assert snap.step_results == {}
    assert snap.spawned_skill_run_ids == {}
