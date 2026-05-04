"""Tier 2 OS invariant tests for eval_builder path resolution.

Guards the Wave 2 fix (eval_builder analyze_skill preprocessor) that prevents
the LLM from constructing filesystem paths. The OS must resolve all paths via
resolve_skill_path; the LLM emits only a short skill name.

Invariants tested:
  - compute_paths resolves stdlib skill names to the correct stdlib path
  - compute_paths resolves local skill names to the correct local path
  - compute_paths accepts both eval_builder_request and user_message artifacts
  - user_message regex extraction handles "skill named <name>" form
  - user_message with unrecognisable text raises ValueError (hard reject)
  - eval_output_path redirects stdlib skills to reyn/local/<name>/eval.md
  - eval_output_path for reyn/local/ skills stays alongside skill.md
  - inject_resolved_paths mirrors _prep into _resolved for LLM use
  - eval_builder skill.md declares compute_paths as trusted python step (B8-NEW-2)
  - eval_builder permissions.python contains a trusted entry for analyze_skill_resolver

Testing policy (docs/ja/contributing/testing.md):
  - No mocks (real instances only)
  - No private-state assertions
  - No algorithm-level pins
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path
from reyn.stdlib.skills.eval_builder.analyze_skill import inject_resolved_paths
from reyn.stdlib.skills.eval_builder.analyze_skill_resolver import compute_paths


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


def _eval_builder_request(target_skill: str) -> dict:
    return {
        "type": "eval_builder_request",
        "data": {"target_skill": target_skill},
    }


def _user_message(text: str) -> dict:
    return {
        "type": "user_message",
        "data": {"text": text},
    }


# ── eval_builder_request form ─────────────────────────────────────────────────


def test_compute_paths_stdlib_via_request_direct_llm(tmp_path, monkeypatch):
    """Tier 2: compute_paths resolves stdlib skill 'direct_llm' via eval_builder_request.

    Guards the core invariant: a short skill name in eval_builder_request.target_skill
    must resolve to the stdlib skill directory, never to a cwd-relative guess.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_eval_builder_request("direct_llm"))

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["skill_dir"] == expected_root
    assert result["skill_dsl_path"] == expected_root + "/skill.md"
    assert result["target_skill"] == "direct_llm"
    assert "stdlib" in result["skill_dir"], (
        "direct_llm must resolve to the stdlib path, not reyn/local/"
    )


def test_compute_paths_stdlib_via_request_skill_improver(tmp_path, monkeypatch):
    """Tier 2: compute_paths resolves stdlib skill 'skill_improver' via eval_builder_request.

    Ensures the resolver works for any stdlib skill, not just direct_llm.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_eval_builder_request("skill_improver"))

    skill_dir, _ = resolve_skill_path("skill_improver")
    expected_root = str(skill_dir).rstrip("/")

    assert result["skill_dir"] == expected_root
    assert result["target_skill"] == "skill_improver"
    assert "stdlib" in result["skill_dir"]


# ── user_message form ─────────────────────────────────────────────────────────


def test_compute_paths_user_message_skill_named_form(tmp_path, monkeypatch):
    """Tier 2: compute_paths extracts skill name from "Generate spec for skill named X".

    The "skill named <name>" regex pattern is the primary form produced by natural
    language CLI usage. It must match the same skill directory as a direct request.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_user_message("Generate spec for skill named direct_llm"))

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["skill_dir"] == expected_root
    assert result["target_skill"] == "direct_llm"
    assert result["skill_dsl_path"] == expected_root + "/skill.md"


def test_compute_paths_user_message_skill_named_skill_improver(tmp_path, monkeypatch):
    """Tier 2: compute_paths handles "skill named skill_improver" in user_message.

    Ensures the regex works for stdlib skills with underscores in the name.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_user_message("Generate spec for skill named skill_improver"))

    skill_dir, _ = resolve_skill_path("skill_improver")
    expected_root = str(skill_dir).rstrip("/")

    assert result["skill_dir"] == expected_root
    assert result["target_skill"] == "skill_improver"


def test_compute_paths_user_message_unrecognised_raises(tmp_path, monkeypatch):
    """Tier 2: compute_paths raises ValueError for unrecognisable user_message text.

    If no skill name can be extracted, the preprocessor must raise ValueError so
    the OS can surface a clear abort rather than silently constructing a bogus path.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Cannot extract skill name"):
        compute_paths(_user_message("なんかランダムなテキスト"))


# ── eval_output_path routing ──────────────────────────────────────────────────


def test_eval_output_path_stdlib_skill_redirects_to_local(tmp_path, monkeypatch):
    """Tier 2: stdlib skills get eval_output_path redirected to reyn/local/<name>/eval.md.

    Stdlib skills live under src/ which is outside the write zone. The resolver
    must redirect the write destination to reyn/local/<name>/eval.md so write_eval
    can write without hitting a [denied] error.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_eval_builder_request("direct_llm"))

    assert result["eval_output_path"] == "reyn/local/direct_llm/eval.md", (
        "stdlib skill eval_output_path must redirect to reyn/local/<name>/eval.md"
    )
    # existing_eval_path still points into stdlib (for reading only)
    assert "stdlib" in result["existing_eval_path"] or "src/" in result["existing_eval_path"]


def test_eval_output_path_local_skill_stays_alongside(tmp_path, monkeypatch):
    """Tier 2: reyn/local/ skills get eval_output_path alongside their skill.md.

    For skills already under reyn/local/, the write destination is skill_dir/eval.md —
    no redirect needed.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "my_local_skill")
    result = compute_paths(_eval_builder_request("my_local_skill"))

    skill_dir, _ = resolve_skill_path("my_local_skill")
    expected = str(skill_dir).rstrip("/") + "/eval.md"

    assert result["eval_output_path"] == expected
    assert "reyn/local/my_local_skill" in result["eval_output_path"]


# ── inject_resolved_paths (pure-mode) ────────────────────────────────────────


def test_inject_resolved_paths_mirrors_prep_into_resolved(tmp_path, monkeypatch):
    """Tier 2: inject_resolved_paths promotes all path fields from data._prep to data._resolved.

    After compute_paths populates data._prep, inject_resolved_paths must mirror all
    eight fields into data._resolved so the LLM can access them without navigating
    the nested _prep structure.
    """
    monkeypatch.chdir(tmp_path)
    _make_local_skill(tmp_path, "mirror_test")

    # Simulate the preprocessor chain: first compute_paths, then inject
    artifact = _eval_builder_request("mirror_test")
    prep_result = compute_paths(artifact)

    # Construct the artifact as the preprocessor engine would after step 1
    artifact_with_prep = {
        "type": "eval_builder_request",
        "data": {
            "target_skill": "mirror_test",
            "_prep": prep_result,
        },
    }
    resolved = inject_resolved_paths(artifact_with_prep)

    for key in [
        "skill_dir", "dsl_root", "target_skill", "skill_dsl_path",
        "phases_glob", "artifacts_glob", "existing_eval_path", "eval_output_path",
    ]:
        assert key in resolved, f"inject_resolved_paths must emit '{key}'"
        assert resolved[key] == prep_result[key], (
            f"data._resolved.{key} must equal data._prep.{key} — no re-derivation"
        )


# ── G17: unknown artifact_type with target_skill field (B8-NEW-6) ─────────────


def test_extract_skill_name_unknown_type_with_target_skill(tmp_path, monkeypatch):
    """Tier 2: _extract_skill_name returns target_skill when artifact_type is "unknown".

    Guards G17 fix: when the LLM omits the "type" field from invoke_skill input,
    the OS assigns artifact_type="unknown". The resolver must still extract the
    skill name from data.target_skill rather than falling through to the
    user_message regex path (which fails because data.text is absent).
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "type": "unknown",
        "data": {"target_skill": "direct_llm"},
    }
    result = compute_paths(artifact)

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["target_skill"] == "direct_llm"
    assert result["skill_dir"] == expected_root
    assert "stdlib" in result["skill_dir"]


def test_extract_skill_name_empty_type_with_target_skill(tmp_path, monkeypatch):
    """Tier 2: _extract_skill_name returns target_skill when artifact type is empty string.

    Guards G17 fix parity: artifact_type="" (artifact.get("type", "") fallback)
    with target_skill present must resolve identically to the "unknown" case.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "data": {"target_skill": "direct_llm"},
    }
    result = compute_paths(artifact)

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["target_skill"] == "direct_llm"
    assert result["skill_dir"] == expected_root


def test_extract_skill_name_eval_builder_request_still_works(tmp_path, monkeypatch):
    """Tier 2: existing eval_builder_request path continues to work after G17 fix.

    Regression guard: the fix must not break the original typed artifact form.
    """
    monkeypatch.chdir(tmp_path)
    result = compute_paths(_eval_builder_request("direct_llm"))

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["target_skill"] == "direct_llm"
    assert result["skill_dir"] == expected_root


def test_extract_skill_name_unknown_type_text_only_falls_back_to_regex(tmp_path, monkeypatch):
    """Tier 2: unknown artifact_type with only "text" field uses regex fallback.

    Guards that the G17 fix does not break the user_message regex path: when
    data has no "target_skill" key but has a parseable "text", extraction succeeds.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "type": "unknown",
        "data": {"text": "Generate spec for skill named direct_llm"},
    }
    result = compute_paths(artifact)

    assert result["target_skill"] == "direct_llm"


def test_extract_skill_name_unknown_type_no_target_skill_no_text_raises(tmp_path, monkeypatch):
    """Tier 2: unknown artifact_type with neither target_skill nor extractable text raises ValueError.

    Guards the error boundary: the resolver must raise ValueError (not silently
    produce a bogus path) when no skill name can be determined from any field.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "type": "unknown",
        "data": {},
    }
    with pytest.raises(ValueError, match="Cannot extract skill name"):
        compute_paths(artifact)


# ── B8-NEW-2: trusted mode declaration (PureModeViolation fix) ────────────────


def _load_eval_builder_skill() -> object:
    """Load the eval_builder Skill object from its installed stdlib path."""
    skill_dir, _ = resolve_skill_path("eval_builder")
    skill_md = Path(skill_dir) / "skill.md"
    return load_dsl_skill(skill_md)


def test_eval_builder_permissions_python_has_trusted_compute_paths():
    """Tier 2: eval_builder skill.md declares compute_paths as mode=trusted (B8-NEW-2 fix).

    Without this declaration the OS falls back to pure mode, causing
    PureModeViolation when analyze_skill_resolver.py imports reyn.skill.skill_paths.
    Guards that the permissions.python block is present and correct.
    """
    skill = _load_eval_builder_skill()

    trusted_entries = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill_resolver.py"
        and p.function == "compute_paths"
        and p.mode == "trusted"
    ]
    assert trusted_entries, (
        "eval_builder skill.md must declare "
        "./analyze_skill_resolver.py:compute_paths with mode=trusted in permissions.python"
    )


def test_eval_builder_permissions_python_inject_resolved_paths_is_pure():
    """Tier 2: eval_builder skill.md declares inject_resolved_paths as mode=pure.

    The pure-mode helper must remain pure (no reyn imports, no I/O).
    Guards that the permissions block does not accidentally escalate it to trusted.
    """
    skill = _load_eval_builder_skill()

    pure_entries = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill.py"
        and p.function == "inject_resolved_paths"
        and p.mode == "pure"
    ]
    assert pure_entries, (
        "eval_builder skill.md must declare "
        "./analyze_skill.py:inject_resolved_paths with mode=pure in permissions.python"
    )
