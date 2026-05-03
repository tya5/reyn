"""Tier 2: OS invariant — SkillSnapshot per-skill crash-recovery state dataclass.

The skill snapshot is the on-disk cache for ``current_phase``,
``last_phase_applied_seq``, ``visit_counts`` and friends — derived state
that the resume runtime keys off. These tests pin the dataclass surface
(``empty()`` / round-trip serialization / schema_version refuse) that
other OS-level code (``SkillRegistry``, ``OSRuntime``) depends on.
"""
import json
from pathlib import Path

import pytest

from reyn.skill.skill_snapshot import SkillSnapshot, SKILL_SNAPSHOT_VERSION


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_path(tmp_path: Path, run_id: str = "run-abc") -> Path:
    return tmp_path / "skills" / f"{run_id}.snapshot.json"


# ── tests ────────────────────────────────────────────────────────────────────

def test_empty_defaults():
    """Tier 2: empty() produces all-default fields for a fresh run."""
    snap = SkillSnapshot.empty("run-001", "my_skill", {"key": "val"})
    assert snap.skill_run_id == "run-001"
    assert snap.skill_name == "my_skill"
    assert snap.skill_input == {"key": "val"}
    assert snap.applied_seq == 0
    assert snap.current_phase == ""
    assert snap.last_phase_artifact_path is None
    assert snap.last_phase_applied_seq == 0
    assert snap.visit_counts == {}
    assert snap.history == []
    assert snap.awaiting_intervention_id is None
    assert snap.last_committed_step_id is None


def test_save_load_roundtrip(tmp_path: Path):
    """Tier 2: save() then load() restores all fields without loss."""
    path = _make_path(tmp_path)
    snap = SkillSnapshot.empty("run-002", "cool_skill", {"a": 1})
    snap.applied_seq = 42
    snap.current_phase = "phase_b"
    snap.last_phase_artifact_path = "artifacts/out.json"
    snap.last_phase_applied_seq = 40
    snap.visit_counts = {"phase_a": 2, "phase_b": 1}
    snap.history = ["phase_a", "phase_b"]
    snap.awaiting_intervention_id = "iv-777"
    snap.last_committed_step_id = "step-003"

    snap.save(path)
    loaded = SkillSnapshot.load("run-002", path)

    assert loaded.skill_run_id == "run-002"
    assert loaded.skill_name == "cool_skill"
    assert loaded.skill_input == {"a": 1}
    assert loaded.applied_seq == 42
    assert loaded.current_phase == "phase_b"
    assert loaded.last_phase_artifact_path == "artifacts/out.json"
    assert loaded.last_phase_applied_seq == 40
    assert loaded.visit_counts == {"phase_a": 2, "phase_b": 1}
    assert loaded.history == ["phase_a", "phase_b"]
    assert loaded.awaiting_intervention_id == "iv-777"
    assert loaded.last_committed_step_id == "step-003"


def test_schema_version_written(tmp_path: Path):
    """Tier 2: saved file contains a 'version' field equal to SKILL_SNAPSHOT_VERSION."""
    path = _make_path(tmp_path, "run-003")
    SkillSnapshot.empty("run-003", "sk", {}).save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == SKILL_SNAPSHOT_VERSION


def test_schema_version_class_var():
    """Tier 2: SCHEMA_VERSION ClassVar equals the module-level constant."""
    assert SkillSnapshot.SCHEMA_VERSION == SKILL_SNAPSHOT_VERSION


def test_load_missing_file_returns_empty(tmp_path: Path):
    """Tier 2: load() on a non-existent path returns a usable empty-ish snapshot."""
    path = tmp_path / "nonexistent.json"
    snap = SkillSnapshot.load("run-404", path)
    # Must be a SkillSnapshot instance with the provided run_id
    assert isinstance(snap, SkillSnapshot)
    assert snap.skill_run_id == "run-404"
    assert snap.applied_seq == 0


def test_load_old_file_missing_new_fields(tmp_path: Path):
    """Tier 2: loading a file without new optional fields falls back to defaults."""
    path = _make_path(tmp_path, "run-old")
    # Simulate a snapshot written before last_committed_step_id was added
    old_payload = {
        "version": 1,
        "skill_run_id": "run-old",
        "skill_name": "legacy_skill",
        "skill_input": {},
        "applied_seq": 5,
        "current_phase": "phase_x",
        # omit: last_phase_artifact_path, last_phase_applied_seq,
        #        visit_counts, history, awaiting_intervention_id,
        #        last_committed_step_id
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old_payload), encoding="utf-8")

    snap = SkillSnapshot.load("run-old", path)
    assert snap.skill_run_id == "run-old"
    assert snap.skill_name == "legacy_skill"
    assert snap.applied_seq == 5
    assert snap.current_phase == "phase_x"
    # New fields fall back to defaults
    assert snap.last_phase_artifact_path is None
    assert snap.last_phase_applied_seq == 0
    assert snap.visit_counts == {}
    assert snap.history == []
    assert snap.awaiting_intervention_id is None
    assert snap.last_committed_step_id is None


def test_atomic_save_uses_tmp_then_replace(tmp_path: Path):
    """Tier 2: save() writes to a .tmp file first, then renames atomically."""
    path = _make_path(tmp_path, "run-atom")
    snap = SkillSnapshot.empty("run-atom", "sk", {})

    # Patch Path.replace to capture the tmp path used
    replaced_from: list[Path] = []
    original_replace = Path.replace

    def _spy_replace(self: Path, target: Path):  # type: ignore[override]
        replaced_from.append(self)
        return original_replace(self, target)

    import unittest.mock as mock
    with mock.patch.object(Path, "replace", _spy_replace):
        snap.save(path)

    assert len(replaced_from) == 1
    tmp_used = replaced_from[0]
    assert tmp_used.suffix == ".tmp" or ".tmp" in tmp_used.name
    # After save, the canonical path exists and tmp is gone
    assert path.exists()
    assert not tmp_used.exists()
