"""Tier 2: OS invariant tests for analyze_skill_resolver_pure (R-PURE-MODE Class D).

Guards the dict-transform contract of resolve_paths_from_op: given the output
of the skill_resolve run_op, it must produce the same eight-field shape that
the legacy unsafe resolve_paths formerly produced, without any fs I/O.

Invariants tested:
  - resolved input -> correct eight-field dict (field-for-field match)
  - unresolved input (resolved=False) -> error dict with null path fields
  - missing _skill_resolved_op key -> treated as unresolved (graceful fallback)
  - skill_root derived from skill_dir by stripping /<target_skill> suffix
  - eval_output_path resolves to .reyn/evals/<name>/eval.md for all skill types
  - eval_output_path is in-zone (.reyn/) — same for stdlib, local, project

Testing policy (docs/deep-dives/contributing/testing.ja.md):
  - No mocks (real instances only)
  - No private-state assertions
  - No algorithm-level pins
"""
from __future__ import annotations

import pytest

from reyn.stdlib.skills.eval_builder.analyze_skill_resolver_pure import (
    resolve_paths_from_op,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact(op_result: dict | None) -> dict:
    """Build an artifact with data._skill_resolved_op set to op_result."""
    artifact: dict = {"data": {}}
    if op_result is not None:
        artifact["data"]["_skill_resolved_op"] = op_result
    return artifact


def _resolved_op(name: str, skill_dir: str) -> dict:
    """Minimal skill_resolve op result for a resolved skill."""
    return {
        "name": name,
        "resolved": True,
        "skill_md_path": skill_dir + "/skill.md",
        "source": "stdlib" if skill_dir.startswith("/") else "local",
        "skill_dir": skill_dir,
    }


def _unresolved_op(name: str) -> dict:
    """Minimal skill_resolve op result for an unresolved skill."""
    return {
        "name": name,
        "resolved": False,
        "skill_md_path": None,
        "source": None,
        "skill_dir": None,
    }


# ---------------------------------------------------------------------------
# Happy path: resolved skill
# ---------------------------------------------------------------------------


def test_resolve_paths_from_op_translates_resolved_input_stdlib():
    """Tier 2: resolved stdlib skill (absolute path) -> correct eight fields.

    Guards the core invariant: when skill_resolve returns resolved=True with an
    absolute skill_dir, resolve_paths_from_op must produce all eight path fields
    matching what the legacy unsafe resolve_paths produced.
    """
    skill_dir = "/absolute/path/to/stdlib/skills/my_skill"
    op_result = _resolved_op("my_skill", skill_dir)
    artifact = _make_artifact(op_result)

    result = resolve_paths_from_op(artifact)

    assert result["skill_dir"] == skill_dir
    assert result["target_skill"] == "my_skill"
    assert result["skill_dsl_path"] == skill_dir + "/skill.md"
    assert result["phases_glob"] == skill_dir + "/phases/*.md"
    assert result["artifacts_glob"] == skill_dir + "/artifacts/*.yaml"
    assert result["existing_eval_path"] == ".reyn/evals/my_skill/eval.md"
    # All skill types write/read eval.md at .reyn/evals/<name>/eval.md (in-zone)
    assert result["eval_output_path"] == ".reyn/evals/my_skill/eval.md"
    # skill_root is parent of skill_dir (strips /<name> suffix)
    assert result["skill_root"] == "/absolute/path/to/stdlib/skills"
    # No error key in happy path
    assert "error" not in result


def test_resolve_paths_from_op_translates_resolved_input_local():
    """Tier 2: resolved local skill (relative path) -> eval paths at .reyn/evals/.

    Guards that local skills also use the canonical .reyn/evals/<name>/eval.md
    location (same as stdlib — all skill types unified, owner directive).
    """
    skill_dir = "reyn/local/my_local_skill"
    op_result = _resolved_op("my_local_skill", skill_dir)
    artifact = _make_artifact(op_result)

    result = resolve_paths_from_op(artifact)

    assert result["skill_dir"] == skill_dir
    assert result["target_skill"] == "my_local_skill"
    assert result["skill_dsl_path"] == skill_dir + "/skill.md"
    assert result["phases_glob"] == skill_dir + "/phases/*.md"
    assert result["artifacts_glob"] == skill_dir + "/artifacts/*.yaml"
    # All skill types: eval.md is at .reyn/evals/<name>/eval.md
    assert result["existing_eval_path"] == ".reyn/evals/my_local_skill/eval.md"
    assert result["eval_output_path"] == ".reyn/evals/my_local_skill/eval.md"
    # skill_root strips /<name> suffix
    assert result["skill_root"] == "reyn/local"


# ---------------------------------------------------------------------------
# Error path: unresolved skill
# ---------------------------------------------------------------------------


def test_resolve_paths_from_op_handles_unresolved_input():
    """Tier 2: unresolved skill_resolve op result -> error dict with null path fields.

    When skill_resolve returns resolved=False (skill not on disk), the pure
    function must return a dict with all path fields as None and an 'error' key.
    The OS will abort the phase on the validation step.
    """
    op_result = _unresolved_op("nonexistent_skill")
    artifact = _make_artifact(op_result)

    result = resolve_paths_from_op(artifact)

    assert result["target_skill"] == "nonexistent_skill"
    assert result["skill_dir"] is None
    assert result["skill_dsl_path"] is None
    assert result["phases_glob"] is None
    assert result["artifacts_glob"] is None
    assert result["existing_eval_path"] is None
    assert result["eval_output_path"] is None
    assert "error" in result
    assert "nonexistent_skill" in result["error"]


def test_resolve_paths_from_op_missing_resolved_treated_as_unresolved():
    """Tier 2: missing _skill_resolved_op key -> graceful fallback as unresolved.

    If the skill_resolve step was skipped (on_error: skip) or the preprocessor
    chain is misconfigured, data._skill_resolved_op will be absent. The pure
    function must treat this the same as resolved=False and not raise.
    """
    # Artifact with no _skill_resolved_op key at all
    artifact: dict = {"data": {"_name": {"target_skill": "some_skill"}}}

    result = resolve_paths_from_op(artifact)

    assert result["skill_dir"] is None
    assert result["skill_dsl_path"] is None
    # Error key present
    assert "error" in result


def test_resolve_paths_from_op_missing_op_result_none():
    """Tier 2: _skill_resolved_op=None -> treated as unresolved.

    When the on_error: empty branch binds None into data._skill_resolved_op,
    the pure function must not raise and must return the error shape.
    """
    artifact = _make_artifact(None)

    result = resolve_paths_from_op(artifact)

    assert result["skill_dir"] is None
    assert "error" in result


# ---------------------------------------------------------------------------
# skill_root derivation
# ---------------------------------------------------------------------------


def test_resolve_paths_from_op_skill_root_strips_name_suffix():
    """Tier 2: skill_root is derived by stripping /<target_skill> from skill_dir.

    The skill-tree root is the parent of the skill's own directory. This
    derivation must be stable for any resolution source (stdlib/local/project).
    """
    skill_dir = "reyn/project/my_project_skill"
    op_result = _resolved_op("my_project_skill", skill_dir)
    artifact = _make_artifact(op_result)

    result = resolve_paths_from_op(artifact)

    assert result["skill_root"] == "reyn/project"


# ---------------------------------------------------------------------------
# permissions.python declaration
# ---------------------------------------------------------------------------


def test_eval_builder_permissions_python_has_safe_resolve_paths_from_op():
    """Tier 2: eval_builder skill.md declares resolve_paths_from_op as mode=safe.

    Guards the R-PURE-MODE Class D invariant: the new pure resolver must be
    declared with mode=safe in the permissions block.
    """
    from pathlib import Path

    from reyn.compiler.loader import load_dsl_skill
    from reyn.skill.skill_paths import resolve_skill_path

    skill_dir, _ = resolve_skill_path("eval_builder")
    skill = load_dsl_skill(Path(skill_dir) / "skill.md")

    safe_entries = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill_resolver_pure.py"
        and p.function == "resolve_paths_from_op"
        and p.mode == "safe"
    ]
    assert safe_entries, (
        "eval_builder skill.md must declare "
        "./analyze_skill_resolver_pure.py:resolve_paths_from_op with mode=safe "
        "in permissions.python (R-PURE-MODE Class D)"
    )
