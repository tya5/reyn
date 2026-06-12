"""Tier 2 OS invariant tests for skill_paths helpers.

Tests introduced by Wave 1 (B6-S1-H1 + B4-M1 fix):

1. ``eval_md_path_for`` — contract: returns .reyn/evals/<name>/eval.md for any
   skill (local/project/stdlib); validates existence via ``resolve_skill_path``
   but path is independent of skill_dir.

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
    stdlib_root,
)
from reyn.stdlib.skills.skill_improver.copy_to_work import extract_skill_name
from reyn.stdlib.skills.skill_improver.copy_to_work_resolver_pure import (
    resolve_paths_from_op,
)


def _categorize_source(skill_dir: Path) -> str | None:
    """Mirror of ``op_runtime.skill_resolve._categorize_source``.

    Replicated here so the test helper can synthesise the
    ``skill_resolve`` run_op output dict without standing up an
    OpContext. The categorisation logic itself is covered separately by
    op_runtime tests; this copy is for self-containment only.
    """
    try:
        skill_dir.resolve().relative_to(stdlib_root().resolve())
        return "stdlib"
    except ValueError:
        pass
    parts = skill_dir.parts
    for i, part in enumerate(parts):
        if part == "reyn" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt == "local":
                return "local"
            if nxt == "project":
                return "project"
    return None


def _synth_skill_resolve_op(name: str) -> dict:
    """Synthesise the ``skill_resolve`` run_op output for ``name``.

    Matches the production op handler at
    ``src/reyn/op_runtime/skill_resolve.py`` so the downstream
    ``resolve_paths_from_op`` sees the same shape it would in a live
    preprocessor.
    """
    try:
        skill_dir, _ = resolve_skill_path(name)
    except (SkillNotFoundError, FileNotFoundError):
        return {
            "name": name,
            "resolved": False,
            "skill_md_path": None,
            "source": None,
            "skill_dir": None,
        }
    source = _categorize_source(skill_dir)
    return {
        "name": name,
        "resolved": True,
        "skill_md_path": str(skill_dir / "skill.md"),
        "source": source,
        "skill_dir": str(skill_dir),
    }


def compute_paths(artifact: dict) -> dict:
    """Test helper: chain extract_skill_name + synth skill_resolve + resolve_paths_from_op.

    Mirrors the active preprocessor in ``phases/copy_to_work.md`` post-
    FP-0042 Phase 2.7 (= the legacy unsafe ``copy_to_work_resolver.py``
    was deleted in the same PR).
    """
    name_result = extract_skill_name(artifact)
    target = name_result["target_skill"]
    op_output = _synth_skill_resolve_op(target)

    enriched = dict(artifact)
    enriched.setdefault("data", {})
    enriched["data"] = dict(enriched["data"])
    enriched["data"]["_name"] = name_result
    # skill_improver/phases/copy_to_work.md binds the op output at data._resolved.
    enriched["data"]["_resolved"] = op_output
    return resolve_paths_from_op(enriched)

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


def test_eval_md_path_for_local_skill(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for returns .reyn/evals/<name>/eval.md for a local skill.

    All skills (local, project, stdlib) use .reyn/evals/<name>/eval.md — the
    canonical in-zone location. Validates skill existence via resolve_skill_path
    but path is independent of skill_dir.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "my_app")

    eval_path = eval_md_path_for("my_app")

    assert eval_path == Path(".reyn") / "evals" / "my_app" / "eval.md", (
        "eval_md_path_for must return .reyn/evals/<name>/eval.md"
    )


def test_eval_md_path_for_stdlib_skill(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for returns .reyn/evals/<name>/eval.md for a stdlib skill.

    Stdlib skills use the same in-zone location as local skills — no redirect
    to reyn/local/ needed (eval.md is not written to the stdlib tree).
    """
    monkeypatch.chdir(tmp_path)
    eval_path = eval_md_path_for("skill_improver")

    assert eval_path == Path(".reyn") / "evals" / "skill_improver" / "eval.md"


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

    assert result["original_skill_root"] == expected_root, (
        "compute_paths must derive original_skill_root from resolve_skill_path"
    )
    assert result["target_skill_root"] == expected_root
    assert result["target_skill_path"] == expected_root + "/skill.md"
    assert result["eval_spec_path"] == ".reyn/evals/direct_llm/eval.md"
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

    assert result["original_skill_root"] == expected_root
    assert "stdlib" in expected_root, (
        "stdlib skill must resolve to the stdlib path, not reyn/local/"
    )


def test_compute_paths_missing_skill_surfaces_error(tmp_path, monkeypatch):
    """Tier 2: compute_paths surfaces an unresolved skill via the resolver dict.

    Post-FP-0042 Phase 2.7 the active preprocessor chain runs the
    ``skill_resolve`` run_op with ``on_error: skip``; the downstream
    safe-mode ``resolve_paths_from_op`` reflects the unresolved op
    output via null fields plus an ``error`` key. The structural
    guarantee (= no LLM-constructed path silently becomes a real path)
    still holds — downstream copy steps see null work_dir / skill_dir
    fields and refuse to proceed.

    Guards B6-S1: hallucinated skill names must NOT produce a bogus path.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "nonexistent_skill_hallucination_xyz"},
    }
    result = compute_paths(artifact)

    assert result.get("target_skill_root") is None
    assert result.get("work_dir") is None
    assert result.get("skill_glob") is None
    assert "skill not found" in (result.get("error") or "").lower()


def test_compute_paths_no_path_in_target_skill_field(tmp_path, monkeypatch):
    """Tier 2: compute_paths must NOT accept path strings in target_skill.

    Post-FP-0042 Phase 2.7 the rejection surfaces as the unresolved
    op-output dict (null fields + ``error`` key) rather than as a raised
    SkillNotFoundError — the active preprocessor chain runs
    ``skill_resolve`` with ``on_error: skip``. The structural guarantee
    still holds: downstream steps refuse to operate on null paths.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "my_app")

    artifact = {
        "type": "improvement_session",
        "data": {"target_skill": "reyn/local/my_app/skill.md"},
    }
    result = compute_paths(artifact)

    assert result.get("target_skill_root") is None
    assert result.get("work_dir") is None
    assert "skill not found" in (result.get("error") or "").lower()


# ── consistency enforcement ─────────────────────────────────────────────────


def test_eval_md_path_consistency(tmp_path, monkeypatch):
    """Tier 2: eval_md_path_for, _derive_eval_output_path, and copy_to_work_resolver
    all return the same .reyn/evals/<name>/eval.md formula for the same skill name.

    Drift-prevention gate: safe-mode preprocessors cannot import eval_md_path_for
    (no reyn imports). They mirror the formula independently. This test enforces
    that all three agree — a single formula change without updating the others fails CI.
    """
    from reyn.stdlib.skills.eval_builder.analyze_skill_resolver_pure import (
        _derive_eval_output_path,
    )
    from reyn.stdlib.skills.skill_improver.copy_to_work_resolver_pure import (
        resolve_paths_from_op as ctw_resolve,
    )

    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "my_skill")
    skill_dir, _ = resolve_skill_path("my_skill")

    # canonical source of truth
    canonical = str(eval_md_path_for("my_skill"))

    # eval_builder preprocessor (write path, any skill_dir_str)
    assert _derive_eval_output_path(str(skill_dir), "my_skill") == canonical, (
        "_derive_eval_output_path must mirror eval_md_path_for formula"
    )

    # copy_to_work preprocessor (read/spec path)
    ctw_resolved = {
        "resolved": True,
        "name": "my_skill",
        "skill_dir": str(skill_dir),
        "error": None,
    }
    ctw_artifact = {"type": "improvement_session", "data": {"_resolved": ctw_resolved}}
    ctw_result = ctw_resolve(ctw_artifact)
    assert ctw_result["eval_spec_path"] == canonical, (
        "copy_to_work_resolver eval_spec_path must mirror eval_md_path_for formula"
    )
