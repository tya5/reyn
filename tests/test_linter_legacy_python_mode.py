"""Tier 2: linter rejects legacy python-mode keywords (FP-0014).

OS invariant: the compiler linter hard-errors when a skill's phase
declares a python preprocessor step whose permission entry uses the
legacy mode keywords (`pure` / `trusted`). The error message must point
the author at the FP-0014 rename so the fix is mechanical.

This pairs with the dataclass / parse-time normalisation in
``reyn.permissions.permissions`` (legacy keywords are silently
canonicalised so already-loaded code keeps running) — the linter is the
user-facing "you must update your YAML" gate.
"""
from __future__ import annotations

from pathlib import Path

from reyn.compiler.linter import lint_phase


def _write_phase(tmp_path: Path, mode_keyword: str) -> Path:
    """Build a minimal phase.md with a python preprocessor step + permission."""
    skill_dir = tmp_path / "my_skill"
    phases_dir = skill_dir / "phases"
    phases_dir.mkdir(parents=True)
    # Trivial helper module so check 2 / 3 do not error first.
    (skill_dir / "helpers.py").write_text(
        "def run(artifact):\n    return artifact\n",
        encoding="utf-8",
    )
    phase = phases_dir / "p.md"
    phase.write_text(
        "---\n"
        "input_schema: {type: object}\n"
        "preprocessor:\n"
        "  - type: python\n"
        "    module: ./helpers.py\n"
        "    function: run\n"
        "    into: data.x\n"
        "    output_schema: {type: object}\n"
        "permissions:\n"
        "  python:\n"
        f"    - {{module: ./helpers.py, function: run, mode: {mode_keyword}}}\n"
        "---\n"
        "instructions\n",
        encoding="utf-8",
    )
    return phase


def test_linter_rejects_legacy_pure_keyword(tmp_path):
    """Tier 2: mode: pure produces a hard error referencing FP-0014."""
    phase = _write_phase(tmp_path, "pure")
    issues = lint_phase(phase, known_artifacts=set())
    errors = [i for i in issues if i.severity == "error"]
    matching = [i for i in errors if "FP-0014" in i.message and "pure" in i.message]
    assert matching, (
        f"Expected an FP-0014 error citing legacy 'pure' keyword, got: "
        f"{[i.message for i in issues]}"
    )


def test_linter_rejects_legacy_trusted_keyword(tmp_path):
    """Tier 2: mode: trusted produces a hard error referencing FP-0014."""
    phase = _write_phase(tmp_path, "trusted")
    issues = lint_phase(phase, known_artifacts=set())
    errors = [i for i in issues if i.severity == "error"]
    matching = [i for i in errors if "FP-0014" in i.message and "trusted" in i.message]
    assert matching, (
        f"Expected an FP-0014 error citing legacy 'trusted' keyword, got: "
        f"{[i.message for i in issues]}"
    )


def test_linter_accepts_new_safe_keyword(tmp_path):
    """Tier 2: mode: safe lints clean (no legacy-keyword error)."""
    phase = _write_phase(tmp_path, "safe")
    issues = lint_phase(phase, known_artifacts=set())
    fp_errors = [
        i for i in issues
        if i.severity == "error" and "FP-0014" in i.message
    ]
    assert not fp_errors, (
        f"mode: safe must not trigger the legacy-keyword error, got: "
        f"{[i.message for i in fp_errors]}"
    )


def test_linter_accepts_new_unsafe_keyword(tmp_path):
    """Tier 2: mode: unsafe lints clean (no legacy-keyword error)."""
    phase = _write_phase(tmp_path, "unsafe")
    issues = lint_phase(phase, known_artifacts=set())
    fp_errors = [
        i for i in issues
        if i.severity == "error" and "FP-0014" in i.message
    ]
    assert not fp_errors, (
        f"mode: unsafe must not trigger the legacy-keyword error, got: "
        f"{[i.message for i in fp_errors]}"
    )
