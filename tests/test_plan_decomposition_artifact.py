"""Tier 2: Plan decomposition artifact write/read/delete (ADR-0023 §3.5).

Step 1 of the Phase 2 migration path. The artifact is the canonical
SSoT for the plan shape on resume; LLM re-decomposition is non-
deterministic and would break step-result memoization.

These tests pin the round-trip + corruption-fallback behavior. No
Session / RouterLoop coupling — the helpers are standalone.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.plan import (
    DECOMPOSITION_SCHEMA_VERSION,
    DecompositionCorruptError,
    decomposition_dir,
    decomposition_path,
    delete_decomposition,
    read_decomposition,
    write_decomposition,
)
from reyn.runtime.planner import Plan, PlanStep


def _sample_plan() -> Plan:
    return Plan(
        goal="Compare README.md and CLAUDE.md",
        steps=(
            PlanStep(
                id="s1",
                description="Read README.md",
                tools=("read_file",),
                depends_on=(),
            ),
            PlanStep(
                id="s2",
                description="Read CLAUDE.md",
                tools=("read_file",),
                depends_on=(),
            ),
            PlanStep(
                id="s3",
                description="Synthesise comparison",
                tools=(),
                depends_on=("s1", "s2"),
            ),
        ),
    )


# ── path helpers ──────────────────────────────────────────────────────────


def test_decomposition_path_layout(tmp_path: Path) -> None:
    """Tier 2: path helpers expose the documented per-plan directory layout."""
    plan_id = "ab12cd34"
    state = tmp_path / "agent_state"
    assert decomposition_dir(state, plan_id) == state / "plans" / plan_id
    assert (
        decomposition_path(state, plan_id)
        == state / "plans" / plan_id / "decomposition.json"
    )


# ── write ─────────────────────────────────────────────────────────────────


def test_write_creates_artifact_with_documented_schema(tmp_path: Path) -> None:
    """Tier 2: write_decomposition produces the ADR-0023 §3.5 schema verbatim."""
    plan = _sample_plan()
    path = write_decomposition(tmp_path, "p001", plan)

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == "p001"
    assert payload["schema_version"] == DECOMPOSITION_SCHEMA_VERSION
    assert payload["goal"] == plan.goal
    step0, step1, step2 = payload["steps"]
    assert step0 == {
        "id": "s1",
        "description": "Read README.md",
        "tools": ["read_file"],
        "depends_on": [],
    }
    assert step1 == {
        "id": "s2",
        "description": "Read CLAUDE.md",
        "tools": ["read_file"],
        "depends_on": [],
    }
    assert step2["depends_on"] == ["s1", "s2"]


def test_write_is_atomic_overwriting_prior_artifact(tmp_path: Path) -> None:
    """Tier 2: re-writing the same plan_id replaces the prior artifact in
    place without leaving the .tmp file behind."""
    plan_v1 = Plan(
        goal="v1",
        steps=(
            PlanStep("a", "first", ()),
            PlanStep("b", "second", ()),
        ),
    )
    plan_v2 = _sample_plan()
    path_v1 = write_decomposition(tmp_path, "p001", plan_v1)
    path_v2 = write_decomposition(tmp_path, "p001", plan_v2)
    assert path_v1 == path_v2
    payload = json.loads(path_v2.read_text(encoding="utf-8"))
    assert payload["goal"] == plan_v2.goal
    step_ids = [s["id"] for s in payload["steps"]]
    assert step_ids == ["s1", "s2", "s3"]
    # No .tmp residue
    assert not path_v2.with_suffix(path_v2.suffix + ".tmp").exists()


# ── read ──────────────────────────────────────────────────────────────────


def test_read_round_trips_to_equivalent_plan(tmp_path: Path) -> None:
    """Tier 2: write → read produces an equivalent Plan."""
    plan = _sample_plan()
    write_decomposition(tmp_path, "p001", plan)
    loaded = read_decomposition(tmp_path, "p001")
    assert loaded.goal == plan.goal
    assert len(loaded.steps) == len(plan.steps)
    for original, recovered in zip(plan.steps, loaded.steps):
        assert recovered.id == original.id
        assert recovered.description == original.description
        assert recovered.tools == original.tools
        assert recovered.depends_on == original.depends_on


def test_read_missing_artifact_raises_filenotfound(tmp_path: Path) -> None:
    """Tier 2: missing artifact surfaces FileNotFoundError so the coordinator
    can fall back to snapshot ``steps_serialized``."""
    with pytest.raises(FileNotFoundError):
        read_decomposition(tmp_path, "nonexistent")


def test_read_invalid_json_raises_corrupt(tmp_path: Path) -> None:
    """Tier 2: malformed JSON surfaces DecompositionCorruptError so the
    coordinator can force action=discard."""
    target = decomposition_path(tmp_path, "p001")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not json {", encoding="utf-8")
    with pytest.raises(DecompositionCorruptError):
        read_decomposition(tmp_path, "p001")


def test_read_wrong_schema_version_raises_corrupt(tmp_path: Path) -> None:
    """Tier 2: schema_version drift refuses load (= future schema bump
    requires explicit migration, not silent best-effort)."""
    target = decomposition_path(tmp_path, "p001")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "plan_id": "p001",
                "schema_version": 999,
                "goal": "g",
                "steps": [{"id": "s1", "description": "d", "tools": [], "depends_on": []}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DecompositionCorruptError, match="schema_version"):
        read_decomposition(tmp_path, "p001")


def test_read_missing_required_fields_raises_corrupt(tmp_path: Path) -> None:
    """Tier 2: structural defects surface as DecompositionCorruptError."""
    target = decomposition_path(tmp_path, "p001")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Missing goal
    target.write_text(
        json.dumps(
            {
                "plan_id": "p001",
                "schema_version": DECOMPOSITION_SCHEMA_VERSION,
                "steps": [{"id": "s", "description": "d", "tools": [], "depends_on": []}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DecompositionCorruptError, match="goal"):
        read_decomposition(tmp_path, "p001")


def test_read_empty_steps_raises_corrupt(tmp_path: Path) -> None:
    """Tier 2: empty steps list is a corruption (= a plan with zero steps
    is not a valid persistence target)."""
    target = decomposition_path(tmp_path, "p001")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "plan_id": "p001",
                "schema_version": DECOMPOSITION_SCHEMA_VERSION,
                "goal": "g",
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DecompositionCorruptError, match="steps"):
        read_decomposition(tmp_path, "p001")


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_removes_existing_artifact(tmp_path: Path) -> None:
    """Tier 2: delete_decomposition returns True on existing artifact and
    removes the file."""
    write_decomposition(tmp_path, "p001", _sample_plan())
    path = decomposition_path(tmp_path, "p001")
    assert path.exists()
    assert delete_decomposition(tmp_path, "p001") is True
    assert not path.exists()


def test_delete_is_idempotent_on_missing_artifact(tmp_path: Path) -> None:
    """Tier 2: delete_decomposition on a non-existent plan returns False
    and does not raise (= safe for AgentRegistry.restore_all cleanup which
    doesn't know if the artifact survived the crash)."""
    assert delete_decomposition(tmp_path, "nonexistent") is False


def test_delete_removes_empty_per_plan_directory(tmp_path: Path) -> None:
    """Tier 2: per-plan directory is cleaned up when empty (= deletion
    leaves no orphan dirs)."""
    write_decomposition(tmp_path, "p001", _sample_plan())
    plan_dir = decomposition_dir(tmp_path, "p001")
    assert plan_dir.is_dir()
    delete_decomposition(tmp_path, "p001")
    assert not plan_dir.exists()


def test_delete_preserves_per_plan_directory_with_other_artifacts(
    tmp_path: Path,
) -> None:
    """Tier 2: per-plan directory survives delete when it contains other
    artifacts (= future Phase 3 step intermediates aren't dropped)."""
    write_decomposition(tmp_path, "p001", _sample_plan())
    plan_dir = decomposition_dir(tmp_path, "p001")
    other = plan_dir / "step_intermediate.json"
    other.write_text("{}", encoding="utf-8")

    delete_decomposition(tmp_path, "p001")

    assert not decomposition_path(tmp_path, "p001").exists()
    assert plan_dir.is_dir()
    assert other.exists()
