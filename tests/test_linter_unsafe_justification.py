"""Tier 2: linter warns on unsafe mode without justification (FP-0014 Component F).

OS invariant: ``lint_skill`` emits a ``warning`` for any user skill whose
``permissions.python`` entry uses ``mode: unsafe`` without an
``unsafe_reason`` field.  Stdlib skills (under ``src/reyn/stdlib/``) are
excluded.  The rule name in every warning message is
``unsafe-without-justification``.
"""
from __future__ import annotations

from pathlib import Path

from reyn.compiler.linter import lint_skill

# ── helpers ───────────────────────────────────────────────────────────────────


def _build_skill(tmp_path: Path, permissions_yaml: str = "") -> Path:
    """Return path to a minimal valid skill.md under *tmp_path*.

    The skill has one phase (`start`) and a trivial artifact so structural
    lint checks don't produce errors that would mask the warning under test.
    """
    skill_dir = tmp_path / "my_skill"
    phases_dir = skill_dir / "phases"
    artifacts_dir = skill_dir / "artifacts"
    phases_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)

    # minimal phase
    (phases_dir / "start.md").write_text(
        "---\ntype: phase\nname: start\n---\nDo the thing.\n",
        encoding="utf-8",
    )

    # minimal artifact referenced as final_output
    (artifacts_dir / "out.yaml").write_text(
        "name: out\nschema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )

    perm_block = f"permissions:\n{permissions_yaml}" if permissions_yaml else ""

    skill_md = skill_dir / "skill.md"
    skill_md.write_text(
        "---\n"
        "type: skill\n"
        "name: my_skill\n"
        "description: test\n"
        "entry: start\n"
        "final_output: out\n"
        "graph:\n"
        "  start: []\n"
        f"{perm_block}"
        "---\n"
        "A test skill.\n",
        encoding="utf-8",
    )
    return skill_md


def _unsafe_warnings(issues):
    return [
        i for i in issues
        if i.severity == "warning" and "unsafe-without-justification" in i.message
    ]


# ── tests ─────────────────────────────────────────────────────────────────────


def test_user_skill_unsafe_no_reason_warns(tmp_path):
    """Tier 2: user skill mode: unsafe without unsafe_reason produces a warning."""
    skill_md = _build_skill(
        tmp_path,
        permissions_yaml=(
            "  python:\n"
            "    - module: ./helper.py\n"
            "      function: run\n"
            "      mode: unsafe\n"
        ),
    )
    issues = lint_skill(skill_md, known_artifacts={"out"})
    warns = _unsafe_warnings(issues)
    assert warns, (
        f"Expected unsafe-without-justification warning, got: "
        f"{[i.message for i in issues]}"
    )


def test_user_skill_unsafe_with_reason_no_warn(tmp_path):
    """Tier 2: user skill mode: unsafe + unsafe_reason produces no warning."""
    skill_md = _build_skill(
        tmp_path,
        permissions_yaml=(
            "  python:\n"
            "    - module: ./helper.py\n"
            "      function: run\n"
            "      mode: unsafe\n"
            "      unsafe_reason: 'Needs network access to external API'\n"
        ),
    )
    issues = lint_skill(skill_md, known_artifacts={"out"})
    warns = _unsafe_warnings(issues)
    assert not warns, (
        f"unsafe_reason present — no warning expected, got: "
        f"{[i.message for i in warns]}"
    )


def test_user_skill_safe_mode_no_warn(tmp_path):
    """Tier 2: user skill mode: safe produces no unsafe-without-justification warning."""
    skill_md = _build_skill(
        tmp_path,
        permissions_yaml=(
            "  python:\n"
            "    - module: ./helper.py\n"
            "      function: run\n"
            "      mode: safe\n"
        ),
    )
    issues = lint_skill(skill_md, known_artifacts={"out"})
    warns = _unsafe_warnings(issues)
    assert not warns, (
        f"mode: safe must not trigger the warning, got: "
        f"{[i.message for i in warns]}"
    )


def test_stdlib_skill_unsafe_no_warn(tmp_path):
    """Tier 2: stdlib skill mode: unsafe without unsafe_reason is NOT warned.

    Stdlib skills (path contains /src/reyn/stdlib/) are excluded from this
    rule — they will be covered by the future ``unsafe-in-stdlib`` hard error.
    """
    # Place the skill inside a path that looks like a stdlib location.
    stdlib_root = tmp_path / "src" / "reyn" / "stdlib" / "skills"
    skill_dir = stdlib_root / "my_stdlib_skill"
    phases_dir = skill_dir / "phases"
    artifacts_dir = skill_dir / "artifacts"
    phases_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)

    (phases_dir / "start.md").write_text(
        "---\ntype: phase\nname: start\n---\nDo the thing.\n",
        encoding="utf-8",
    )
    (artifacts_dir / "out.yaml").write_text(
        "name: out\nschema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )

    skill_md = skill_dir / "skill.md"
    skill_md.write_text(
        "---\n"
        "type: skill\n"
        "name: my_stdlib_skill\n"
        "description: stdlib test\n"
        "entry: start\n"
        "final_output: out\n"
        "graph:\n"
        "  start: []\n"
        "permissions:\n"
        "  python:\n"
        "    - module: ./helper.py\n"
        "      function: run\n"
        "      mode: unsafe\n"
        "---\n"
        "A stdlib skill.\n",
        encoding="utf-8",
    )

    issues = lint_skill(skill_md, known_artifacts={"out"})
    warns = _unsafe_warnings(issues)
    assert not warns, (
        f"Stdlib skill must not trigger unsafe-without-justification, got: "
        f"{[i.message for i in warns]}"
    )
