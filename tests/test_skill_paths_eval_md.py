"""Tier 2 OS invariant tests for skill_paths helpers.

Tests introduced by Wave 1 (B6-S1-H1 + B4-M1 fix):

1. ``eval_md_path_for`` — contract: returns <skill_dir>/eval.md derived via
   ``resolve_skill_path``; both prepare (reader) and eval_builder (writer)
   MUST use this helper so path mismatch is structurally impossible.

2. ``compute_paths`` resolver contract — the copy_to_work preprocessor must
   derive all paths from ``target_skill`` (a short name) via ``resolve_skill_path``,
   never from an LLM-constructed path string.

Tier classification: these tests pin the invariant that path derivation is
centralised in the OS resolver (= OS invariant), not in the LLM output.
They are Tier 2 tests, not Tier 1 (not a single-function API contract test)
because they guard the *system-level* guarantee that no path string from the
LLM ever reaches the filesystem.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.skill.skill_paths import (
    SkillNotFoundError,
    eval_md_path_for,
    resolve_skill_path,
)
from reyn.stdlib.skills.skill_improver.copy_to_work_resolver import compute_paths


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_local_skill(tmp_path: Path, name: str) -> Path:
    """Create a minimal local skill under tmp_path/reyn/local/<name>/."""
    root = tmp_path / "reyn" / "local" / name
    (root / "phases").mkdir(parents=True)
    (root / "skill.md").write_text(
        "---\ntype: skill\nname: fake\nentry: go\nfinal_output: user_message\n"
        "graph:\n  go: []\n---\n",
        encoding="utf-8",
    )
    return root


# ── eval_md_path_for invariants ────────────────────────────────────────────────


def test_eval_md_path_for_derives_from_resolve_skill_path(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for returns skill_dir/eval.md, same root as resolve_skill_path.

    Guards B4-M1: the eval.md path used by prepare (reader) and eval_builder
    (writer) must be structurally identical — both derived by the same helper.
    """
    monkeypatch.chdir(tmp_path)
    skill_dir = _make_local_skill(tmp_path, "my_app")

    resolved_dir, _ = resolve_skill_path("my_app")
    eval_path = eval_md_path_for("my_app")

    assert eval_path == resolved_dir / "eval.md", (
        "eval_md_path_for must derive path from resolve_skill_path — mismatch is B4-M1"
    )
    assert str(eval_path).endswith("/my_app/eval.md"), (
        "eval.md must be directly under the skill directory"
    )


def test_eval_md_path_for_stdlib_skill(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for resolves stdlib skills to the stdlib path.

    Stdlib skills live under src/reyn/stdlib/skills/<name>/.  eval_md_path_for
    must return that path (writes are forbidden there by the permission system —
    callers that need to *write* must redirect to reyn/local/<name>/eval.md).
    """
    # stdlib path is absolute (no chdir needed for the stdlib itself)
    monkeypatch.chdir(tmp_path)
    # skill_improver is a real stdlib skill — use it as the test subject
    skill_dir, _ = resolve_skill_path("skill_improver")
    eval_path = eval_md_path_for("skill_improver")

    assert eval_path == skill_dir / "eval.md"
    # The stdlib path must be inside the package, not in reyn/local
    assert "stdlib" in str(eval_path), (
        "stdlib skill eval.md path must resolve inside the stdlib tree"
    )


def test_eval_md_path_for_missing_skill_raises(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for raises SkillNotFoundError for unknown skill names.

    Ensures callers get a clear error rather than silently constructing a
    wrong path.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SkillNotFoundError) as exc_info:
        eval_md_path_for("definitely_nonexistent_skill_xyz_wave1")
    assert "definitely_nonexistent_skill_xyz_wave1" in str(exc_info.value)


def test_eval_md_path_for_is_caught_by_except_exception(tmp_path, monkeypatch):
    """Tier 2: SkillNotFoundError from eval_md_path_for is catchable by `except Exception`.

    Same contract as resolve_skill_path — the op-runtime's generic handler
    must catch the error and surface it as status='error', not let it escape.
    """
    monkeypatch.chdir(tmp_path)
    try:
        eval_md_path_for("missing_skill_wave1")
    except Exception as exc:
        assert isinstance(exc, SkillNotFoundError)
        return
    pytest.fail("Expected SkillNotFoundError")


# ── compute_paths resolver contract ────────────────────────────────────────────


def test_compute_paths_uses_resolve_skill_path(tmp_path, monkeypatch):
    """Tier 2: compute_paths derives all paths from target_skill via resolve_skill_path.

    Guards B6-S1-H1: the LLM emits only target_skill (a name, no slashes).
    The preprocessor must use the OS resolver, not any LLM-supplied path string.
    The resolved paths must point to the actual skill directory found by resolve_skill_path.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "direct_llm")

    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "direct_llm"},
    }
    result = compute_paths(artifact)

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["original_dsl_root"] == expected_root, (
        "compute_paths must derive original_dsl_root from resolve_skill_path"
    )
    assert result["target_dsl_root"] == expected_root
    assert result["target_skill_path"] == expected_root + "/skill.md"
    assert result["eval_spec_path"] == expected_root + "/eval.md"
    assert result["work_dir"] == ".reyn/skill_improver_work/direct_llm"


def test_compute_paths_stdlib_skill(tmp_path, monkeypatch):
    """Tier 2: compute_paths resolves a stdlib skill name to the correct stdlib path.

    When the user asks to improve a stdlib skill (e.g. "direct_llm" found in
    stdlib), compute_paths must resolve it via resolve_skill_path — not via any
    hardcoded "reyn/local/<name>" prefix.
    """
    monkeypatch.chdir(tmp_path)
    # skill_improver is a real stdlib skill
    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "skill_improver"},
    }
    result = compute_paths(artifact)

    skill_dir, _ = resolve_skill_path("skill_improver")
    expected_root = str(skill_dir).rstrip("/")

    assert result["original_dsl_root"] == expected_root
    assert "stdlib" in expected_root, (
        "stdlib skill must resolve to the stdlib path, not reyn/local/"
    )


def test_compute_paths_missing_skill_raises(tmp_path, monkeypatch):
    """Tier 2: compute_paths raises SkillNotFoundError for unknown skill names.

    If the LLM hallucinates a non-existent skill name, compute_paths must
    raise rather than silently constructing a bogus path (which would cause
    copy 0 → workspace empty → eval FileNotFoundError, as in B6-S1).
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "nonexistent_skill_hallucination_xyz"},
    }
    with pytest.raises(SkillNotFoundError):
        compute_paths(artifact)


def test_compute_paths_no_path_in_target_skill_field(tmp_path, monkeypatch):
    """Tier 2: compute_paths must NOT accept path strings in target_skill.

    If a path string slips through (e.g. "reyn/local/my_app/skill.md"), the
    resolver must fail with SkillNotFoundError — the LLM must not be able to
    supply a path.  This guards the structural boundary: target_skill is a
    name, not a path.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "my_app")

    # Hallucinated path string — must be rejected by the resolver
    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "reyn/local/my_app/skill.md"},
    }
    with pytest.raises(SkillNotFoundError):
        compute_paths(artifact)
