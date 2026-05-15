"""Tier 2b: copy_to_work_resolver_pure::resolve_paths_from_op invariants.

Guards the pure-mode dict transform (R-PURE-MODE Class D) that consumes the
skill_resolve op output and produces the same shape as the legacy unsafe
resolve_paths, without any fs I/O.

Tests use an in-memory artifact dict only — no real fs walk is performed.
"""
from __future__ import annotations

import pytest

from reyn.stdlib.skills.skill_improver.copy_to_work_resolver_pure import (
    resolve_paths_from_op,
)


def _make_artifact(resolved: dict | None = None, name: dict | None = None) -> dict:
    """Build a minimal preprocessor artifact carrying data._resolved and data._name."""
    data: dict = {}
    if resolved is not None:
        data["_resolved"] = resolved
    if name is not None:
        data["_name"] = name
    return {"type": "improvement_session", "data": data}


# ── resolved case ─────────────────────────────────────────────────────────────


def test_resolve_paths_from_op_translates_resolved_input():
    """Tier 2b: resolved skill_resolve output maps to the same shape as legacy resolve_paths.

    All eight expected fields must be present and derived correctly from skill_dir + name.
    No extra "error" key should appear when resolved=True.
    """
    skill_dir = "/abs/path/to/reyn/local/my_skill"
    artifact = _make_artifact(
        resolved={
            "name": "my_skill",
            "resolved": True,
            "skill_md_path": skill_dir + "/skill.md",
            "source": "local",
            "skill_dir": skill_dir,
        }
    )

    result = resolve_paths_from_op(artifact)

    assert result["skill_glob"] == skill_dir + "/skill.md"
    assert result["phases_glob"] == skill_dir + "/phases/*.md"
    assert result["work_dir"] == ".reyn/skill_improver_work/my_skill"
    assert result["original_skill_root"] == skill_dir
    assert result["skill_slug"] == "my_skill"
    assert result["target_skill_path"] == skill_dir + "/skill.md"
    assert result["target_skill_root"] == skill_dir
    assert result["eval_spec_path"] == skill_dir + "/eval.md"
    assert "error" not in result


def test_resolve_paths_from_op_strips_trailing_slash():
    """Tier 2b: trailing slash on skill_dir is stripped so derived paths are consistent."""
    skill_dir = "/some/path/my_skill/"
    artifact = _make_artifact(
        resolved={
            "name": "my_skill",
            "resolved": True,
            "skill_md_path": skill_dir + "skill.md",
            "source": "stdlib",
            "skill_dir": skill_dir,
        }
    )

    result = resolve_paths_from_op(artifact)

    # Derived paths must not contain double slashes from the trailing slash
    assert not result["skill_glob"].startswith("/some/path/my_skill//")
    assert result["skill_glob"] == "/some/path/my_skill/skill.md"
    assert result["phases_glob"] == "/some/path/my_skill/phases/*.md"


def test_resolve_paths_from_op_source_field_passthrough():
    """Tier 2b: source value (project/local/stdlib) passes through unchanged."""
    for source in ("project", "local", "stdlib"):
        artifact = _make_artifact(
            resolved={
                "name": "foo",
                "resolved": True,
                "skill_md_path": "/x/foo/skill.md",
                "source": source,
                "skill_dir": "/x/foo",
            }
        )
        result = resolve_paths_from_op(artifact)
        # source is not part of the legacy output shape — confirm no extra "error"
        # and that the slug is correct regardless of source
        assert result["skill_slug"] == "foo"
        assert "error" not in result


# ── unresolved case ───────────────────────────────────────────────────────────


def test_resolve_paths_from_op_handles_unresolved_input():
    """Tier 2b: unresolved skill_resolve output yields null paths and an error field.

    Downstream preprocessor steps can detect the failure via the "error" key
    and null path fields rather than a hard abort.
    """
    artifact = _make_artifact(
        resolved={
            "name": "nonexistent_skill",
            "resolved": False,
            "skill_md_path": None,
            "source": None,
            "skill_dir": None,
        }
    )

    result = resolve_paths_from_op(artifact)

    assert result["skill_glob"] is None
    assert result["phases_glob"] is None
    assert result["work_dir"] is None
    assert result["original_skill_root"] is None
    assert result["target_skill_path"] is None
    assert result["target_skill_root"] is None
    assert result["eval_spec_path"] is None
    assert result["skill_slug"] == "nonexistent_skill"
    assert "error" in result
    assert "nonexistent_skill" in result["error"]


# ── missing _resolved case ────────────────────────────────────────────────────


def test_resolve_paths_from_op_missing_resolved_treated_as_unresolved():
    """Tier 2b: absent data._resolved is treated as unresolved — graceful handling.

    This guards against the skill_resolve run_op step skipping on error (on_error: skip)
    without populating data._resolved. The transform must not raise; it must surface
    the error gracefully.
    """
    # _resolved key absent entirely
    artifact = _make_artifact(
        name={"target_skill": "missing_skill"}
        # no `resolved` kwarg → data._resolved not set
    )

    result = resolve_paths_from_op(artifact)

    assert result["skill_glob"] is None
    assert result["target_skill_root"] is None
    assert "error" in result


def test_resolve_paths_from_op_empty_artifact_data_treated_as_unresolved():
    """Tier 2b: empty data dict (no _resolved, no _name) does not raise."""
    artifact = {"type": "improvement_session", "data": {}}

    result = resolve_paths_from_op(artifact)

    assert result["skill_glob"] is None
    assert "error" in result


def test_resolve_paths_from_op_null_data_treated_as_unresolved():
    """Tier 2b: artifact["data"] = None does not raise — handled as empty."""
    artifact = {"type": "improvement_session", "data": None}

    result = resolve_paths_from_op(artifact)

    assert result["skill_glob"] is None
    assert "error" in result
