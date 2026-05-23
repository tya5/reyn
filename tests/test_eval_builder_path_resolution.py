"""Tier 2 OS invariant tests for eval_builder path resolution.

Guards the Wave 2 fix (eval_builder analyze_skill preprocessor) that prevents
the LLM from constructing filesystem paths. The OS must resolve all paths via
``resolve_skill_path``; the LLM emits only a short skill name.

FP-0042 Phase 2.5 (2026-05-23): the legacy unsafe ``resolve_paths`` in
``analyze_skill_resolver.py`` was deleted. The active preprocessor chain
in ``phases/analyze_skill.md`` already uses the ``skill_resolve`` run_op
(= OS-level fs walk) plus the safe-mode ``resolve_paths_from_op`` pure
transform. The ``_compute_paths`` helper below now mirrors that chain
exactly — it calls ``resolve_skill_path`` directly to synthesise the
``skill_resolve`` op output dict, then feeds it through the same
``resolve_paths_from_op`` the OS runs in production.

Invariants tested:
  - The full chain (extract_skill_name → synth-op → resolve_paths_from_op)
    resolves stdlib + local skills to the correct path
  - extract_skill_name accepts both eval_builder_request and user_message
    artifacts (top-level / wrapped / regex-fallback shapes)
  - user_message with unrecognisable text raises ValueError (hard reject)
  - eval_output_path redirects stdlib skills to reyn/local/<name>/eval.md
  - eval_output_path for reyn/local/ skills stays alongside skill.md
  - inject_resolved_paths mirrors _prep into _resolved for LLM use
  - eval_builder skill.md declares all 3 python steps as mode=safe

Testing policy (docs/deep-dives/contributing/testing.ja.md):
  - No mocks (real instances only)
  - No private-state assertions
  - No algorithm-level pins
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root
from reyn.stdlib.skills.eval_builder.analyze_skill import (
    extract_skill_name,
    inject_resolved_paths,
)
from reyn.stdlib.skills.eval_builder.analyze_skill_resolver_pure import (
    resolve_paths_from_op,
)


def _categorize_source(skill_dir: Path) -> str | None:
    """Mirror of ``op_runtime.skill_resolve._categorize_source``.

    Replicated here so the test helper can build a synthetic op output
    without standing up an OpContext. The categorisation logic itself is
    covered separately by the op_runtime tests; this copy is purely to
    keep the helper self-contained.
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

    Matches the production op handler at ``src/reyn/op_runtime/skill_resolve.py``
    so the downstream ``resolve_paths_from_op`` sees the same shape it would
    in a live preprocessor.
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


def _compute_paths(artifact: dict) -> dict:
    """Test helper: chain extract_skill_name + synth skill_resolve + resolve_paths_from_op.

    Mirrors the active preprocessor in ``phases/analyze_skill.md``:

      1. ``extract_skill_name`` (safe-mode python) — pure dict / regex
      2. ``skill_resolve`` run_op (OS layer) — synthesised here via the
         same logic the op handler uses
      3. ``resolve_paths_from_op`` (safe-mode python) — pure dict transform
    """
    name_result = extract_skill_name(artifact)
    target = name_result["target_skill"]

    op_output = _synth_skill_resolve_op(target)

    enriched = dict(artifact)
    enriched["data"] = dict(enriched.get("data") or {})
    enriched["data"]["_name"] = name_result
    enriched["data"]["_skill_resolved_op"] = op_output

    return resolve_paths_from_op(enriched)


# Keep the old alias so the per-test call sites below stay unchanged.
compute_paths = _compute_paths

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
        "skill_dir", "skill_root", "target_skill", "skill_dsl_path",
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


# ── B9-NEW-2: G17 wrong-layer fix — top-level target_skill (runtime shape) ────


def test_extract_skill_name_top_level_target_skill(tmp_path, monkeypatch):
    """Tier 2: _extract_skill_name reads target_skill from artifact top level.

    Guards B9-NEW-2 fix: at runtime the OS passes the invoke_skill input dict
    directly as the artifact (no `data` wrapper). The B9-S5b retest observed
    the actual shape `{"target_skill": "direct_llm", "eval_spec": {...}}`.

    The resolver must extract from artifact["target_skill"] (top level), not
    only from artifact["data"]["target_skill"] (wrapped form).
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "target_skill": "direct_llm",
        "eval_spec": {"name": "direct_llm.md"},
    }
    result = compute_paths(artifact)

    skill_dir, _ = resolve_skill_path("direct_llm")
    expected_root = str(skill_dir).rstrip("/")

    assert result["target_skill"] == "direct_llm"
    assert result["skill_dir"] == expected_root
    assert "stdlib" in result["skill_dir"]


def test_extract_skill_name_top_level_target_skill_minimal(tmp_path, monkeypatch):
    """Tier 2: top-level target_skill alone (no eval_spec) resolves correctly.

    Minimal runtime shape produced by `invoke_skill(input={"target_skill": "..."})`
    when the LLM omits all other fields — the resolver must still extract the
    name without requiring sibling fields.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {"target_skill": "direct_llm"}
    result = compute_paths(artifact)

    assert result["target_skill"] == "direct_llm"
    skill_dir, _ = resolve_skill_path("direct_llm")
    assert result["skill_dir"] == str(skill_dir).rstrip("/")


def test_extract_skill_name_top_level_takes_priority_over_data(tmp_path, monkeypatch):
    """Tier 2: when both top-level and data.target_skill are present, top-level wins.

    Edge case: if some upstream layer happens to put the field in both places,
    the top-level form (= the OS runtime shape) is authoritative.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {
        "target_skill": "direct_llm",  # top-level (priority 1)
        "data": {"target_skill": "skill_improver"},  # wrapped (priority 2)
    }
    result = compute_paths(artifact)

    assert result["target_skill"] == "direct_llm"


def test_extract_skill_name_top_level_empty_string_raises(tmp_path, monkeypatch):
    """Tier 2: top-level target_skill present but empty string raises ValueError.

    Guards the empty-name boundary at the top-level priority path.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {"target_skill": ""}
    with pytest.raises(ValueError, match="empty top-level 'target_skill'"):
        compute_paths(artifact)


def test_extract_skill_name_top_level_text_fallback(tmp_path, monkeypatch):
    """Tier 2: top-level text (no target_skill, no data wrapper) feeds regex fallback.

    Some OS paths may emit `{"text": "..."}` at the top level instead of the
    wrapped `{"data": {"text": "..."}}` form. Regex fallback should accept
    both shapes.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {"text": "Generate spec for skill named direct_llm"}
    result = compute_paths(artifact)

    assert result["target_skill"] == "direct_llm"


# ── B8-NEW-2: trusted mode declaration (PureModeViolation fix) ────────────────


def _load_eval_builder_skill() -> object:
    """Load the eval_builder Skill object from its installed stdlib path."""
    skill_dir, _ = resolve_skill_path("eval_builder")
    skill_md = Path(skill_dir) / "skill.md"
    return load_dsl_skill(skill_md)


def test_eval_builder_permissions_python_all_steps_safe():
    """Tier 2: eval_builder skill.md declares every python step as mode=safe.

    Post-FP-0042 Phase 2.5 (= legacy unsafe ``analyze_skill_resolver.resolve_paths``
    deleted), every python step in eval_builder runs safe-mode. Filesystem-
    touching path resolution is delegated to the ``skill_resolve`` run_op in
    the preprocessor chain (= R-PURE-MODE-REDEFINE Class D).
    """
    skill = _load_eval_builder_skill()

    modes = {(p.module, p.function): p.mode for p in skill.permissions.python}
    expected = {
        ("./analyze_skill.py", "extract_skill_name"): "safe",
        ("./analyze_skill_resolver_pure.py", "resolve_paths_from_op"): "safe",
        ("./analyze_skill.py", "inject_resolved_paths"): "safe",
    }
    for key, expected_mode in expected.items():
        assert modes.get(key) == expected_mode, (
            f"{key[0]}:{key[1]} must be mode={expected_mode}, "
            f"got {modes.get(key)!r}"
        )


def test_eval_builder_permissions_python_legacy_unsafe_resolver_removed():
    """Tier 2: regression guard — the legacy
    ``./analyze_skill_resolver.py:resolve_paths`` permission entry must
    not reappear (FP-0042 Phase 2.5 deleted the module). Any future patch
    that re-adds it should also restore the file + adjust the AST guard."""
    skill = _load_eval_builder_skill()

    legacy = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill_resolver.py"
    ]
    assert legacy == [], (
        "Legacy unsafe resolver permission entry must stay removed. "
        f"Found: {legacy}"
    )


def test_eval_builder_permissions_python_extract_skill_name_is_safe():
    """Tier 2: eval_builder skill.md declares extract_skill_name as mode=safe.

    extract_skill_name is pure dict + regex — no I/O, no reyn imports.
    Guards that the R-PURE-MODE-REDEFINE Class B refactor correctly declares
    the new safe step.
    """
    skill = _load_eval_builder_skill()

    safe_entries = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill.py"
        and p.function == "extract_skill_name"
        and p.mode == "safe"
    ]
    assert safe_entries, (
        "eval_builder skill.md must declare "
        "./analyze_skill.py:extract_skill_name with mode=safe in permissions.python"
    )


def test_eval_builder_permissions_python_inject_resolved_paths_is_safe():
    """Tier 2: eval_builder skill.md declares inject_resolved_paths as mode=safe.

    The safe-mode helper must remain safe (no reyn imports, no I/O).
    Guards that the permissions block does not accidentally escalate it to unsafe.

    FP-0014: stdlib YAML still says `mode: pure` (Track B will rename those);
    PermissionDecl normalises legacy keywords at parse time so the loaded
    mode reads as the new keyword `safe`.
    """
    skill = _load_eval_builder_skill()

    safe_entries = [
        p for p in skill.permissions.python
        if p.module == "./analyze_skill.py"
        and p.function == "inject_resolved_paths"
        and p.mode == "safe"
    ]
    assert safe_entries, (
        "eval_builder skill.md must declare "
        "./analyze_skill.py:inject_resolved_paths with mode=safe in permissions.python"
    )
