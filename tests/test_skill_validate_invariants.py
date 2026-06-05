"""Tier 2: op/permission cross-layer consistency invariants (FP-0026).

OS invariant: the validator detects when a phase uses a Tier 2-3 op in
``allowed_ops`` without the matching skill-level permission declaration.
Four tests cover the four cases specified in the feature proposal.
"""
from __future__ import annotations

import logging
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────


def _build_skill_dir(
    tmp_path: Path,
    *,
    skill_permissions_yaml: str = "",
    phase_allowed_ops: list[str] | None = None,
) -> Path:
    """Build a minimal skill directory suitable for validate_skill_dir.

    Returns the skill_dir (containing skill.md + phases/).
    """
    skill_dir = tmp_path / "test_skill"
    phases_dir = skill_dir / "phases"
    artifacts_dir = skill_dir / "artifacts"
    phases_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)

    # Minimal artifact
    (artifacts_dir / "out.yaml").write_text(
        "name: out\nschema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )

    # Phase with the given allowed_ops
    ops_yaml = ""
    if phase_allowed_ops is not None:
        items = ", ".join(phase_allowed_ops)
        ops_yaml = f"allowed_ops: [{items}]\n"

    (phases_dir / "start.md").write_text(
        "---\n"
        "type: phase\n"
        "name: start\n"
        f"{ops_yaml}"
        "---\n"
        "Do the thing.\n",
        encoding="utf-8",
    )

    perm_block = f"permissions:\n{skill_permissions_yaml}" if skill_permissions_yaml else ""

    (skill_dir / "skill.md").write_text(
        "---\n"
        "type: skill\n"
        "name: test_skill\n"
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

    return skill_dir


# ── test 1 — undeclared permission → ERROR ────────────────────────────────────


def test_validate_detects_undeclared_permission(tmp_path: Path) -> None:
    """Tier 2: phase allowed_ops includes 'mcp' but skill.permissions has no mcp declaration.

    Invariant: validate_skill_dir returns a ValidationResult with at least one
    ERROR whose op_kind is 'mcp'.  ok must be False. (#1352-A: migrated off the
    removed `shell` Tier-2/3 gate to `mcp`, the canonical remaining gate.)
    """
    from reyn.skill.validator import validate_skill_dir

    skill_dir = _build_skill_dir(
        tmp_path,
        skill_permissions_yaml="",  # no permissions block at all
        phase_allowed_ops=["mcp"],
    )
    result = validate_skill_dir(skill_dir)
    assert not result.ok, "Expected validation to fail when mcp undeclared"
    error_ops = {e.op_kind for e in result.errors}
    assert "mcp" in error_ops, (
        f"Expected error for 'mcp', got errors: {[e.message for e in result.errors]}"
    )


# ── test 2 — consistent skill → OK ───────────────────────────────────────────


def test_validate_passes_consistent_skill(tmp_path: Path) -> None:
    """Tier 2: skill.permissions.shell = true and phase allowed_ops includes 'shell' → no errors.

    Invariant: a fully consistent skill passes with ok=True and no errors.
    Warnings for dead declarations are acceptable but not expected here.
    """
    from reyn.skill.validator import validate_skill_dir

    skill_dir = _build_skill_dir(
        tmp_path,
        skill_permissions_yaml="  shell: true\n",
        phase_allowed_ops=["shell"],
    )
    result = validate_skill_dir(skill_dir)
    assert result.ok, (
        f"Expected OK, got errors: {[e.message for e in result.errors]}"
    )
    assert not result.errors


# ── test 3 — dead permission declaration → WARNING ────────────────────────────


def test_validate_detects_dead_permission_declaration(tmp_path: Path) -> None:
    """Tier 2: skill.permissions.mcp declared but no phase lists 'mcp' in allowed_ops → WARNING.

    Invariant: validate_skill_dir returns a ValidationResult with ok=True
    (no errors) and at least one WARNING for the dead 'mcp' declaration.
    (#1352-A: migrated off the removed `shell` Tier-2/3 gate to `mcp`.)
    """
    from reyn.skill.validator import validate_skill_dir

    skill_dir = _build_skill_dir(
        tmp_path,
        skill_permissions_yaml="  mcp:\n    - github\n",
        phase_allowed_ops=["file"],  # mcp declared but not used in any phase
    )
    result = validate_skill_dir(skill_dir)
    # No errors expected (declaration exists for what's used; mcp is declared
    # but file doesn't require a permission, so no error).
    assert result.ok, (
        f"Expected no errors, got: {[e.message for e in result.errors]}"
    )
    # Warning for dead mcp declaration.
    warning_ops = {w.op_kind for w in result.warnings}
    assert "mcp" in warning_ops, (
        f"Expected warning for dead 'mcp' declaration, got warnings: "
        f"{[w.message for w in result.warnings]}"
    )


# ── test 4 — load-time warning via logger ────────────────────────────────────


def test_skill_load_warns_on_inconsistency(tmp_path: Path, caplog) -> None:
    """Tier 2: loading a skill with op/permission inconsistency emits a logger.warning.

    Invariant: load_dsl_skill completes successfully (no raise) AND at least one
    WARNING-level log message appears in reyn.compiler.loader for the inconsistent
    skill.  No mocks used — real skill fixture + real loader.
    """
    from reyn.compiler.loader import load_dsl_skill

    skill_dir = _build_skill_dir(
        tmp_path,
        skill_permissions_yaml="",          # no permissions declared
        phase_allowed_ops=["mcp_install"],  # Tier 3 op without permission
    )

    # Need real artifact files that load_dsl_skill can resolve.
    # The _build_skill_dir helper already creates artifacts/out.yaml.
    skill_md = skill_dir / "skill.md"

    with caplog.at_level(logging.WARNING, logger="reyn.compiler.loader"):
        # Must not raise — load completes even with inconsistency.
        skill = load_dsl_skill(skill_md, skill_root=skill_dir.parent)

    assert skill is not None, "Expected skill to load successfully"
    warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("mcp_install" in msg for msg in warning_texts), (
        f"Expected warning mentioning 'mcp_install', got: {warning_texts}"
    )
