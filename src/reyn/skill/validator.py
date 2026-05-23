"""Op/Permission cross-layer consistency validator (FP-0026).

Checks that phase ``allowed_ops`` declarations are consistent with the
skill-level ``permissions`` block.  Two violation classes:

ERROR — phase uses a Tier 2-3 op kind but the matching permission is not
declared.  At runtime the op will raise ``PermissionError`` before any work
is done.  This is a correctness defect that the skill author must fix.

WARNING — skill declares a permission that is never referenced by any
phase's ``allowed_ops``.  This is a dead declaration: harmless but noisy
(the startup guard may still prompt the user unnecessarily).

Supported Tier 2-3 op kinds (ops that need an explicit permission entry):

  ``shell``       → ``permissions.shell: true``
  ``mcp``         → ``permissions.mcp: [server, ...]``  (non-empty list)
  ``mcp_install`` → ``permissions.mcp_install: true``
  ``index_drop``  → ``permissions.index_drop: true``

All other op kinds (file, run_skill, lint, ask_user, web_fetch, web_search,
embed, index_write, index_query, recall, sandboxed_exec) have default
permissibility and do not require an explicit permissions entry to function.
``file`` has fine-grained path declarations (file.read / file.write) that are
out of scope for this cross-check; they are handled by the startup guard at
runtime.

Usage::

    from reyn.skill.validator import validate_skill_dir, ValidationResult
    result = validate_skill_dir(skill_dir)
    if result.errors:
        sys.exit(1)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Op kinds that require an explicit permissions entry (Tier 2-3).
# Mapping: op_kind → human-readable permission key and fix hint.
# ---------------------------------------------------------------------------

_TIER23_GATES: dict[str, dict] = {
    "shell": {
        "label": "permissions.shell",
        "fix": "Add `permissions:\\n  shell: true` to the skill.md frontmatter.",
    },
    "mcp": {
        "label": "permissions.mcp",
        "fix": (
            "Add `permissions:\\n  mcp: [<server_name>]` to the skill.md frontmatter, "
            "listing every MCP server this skill calls."
        ),
    },
    "mcp_install": {
        "label": "permissions.file.write[.reyn/mcp.yaml]",
        "fix": (
            "Add `permissions:\\n  file.write:\\n    - path: .reyn/mcp.yaml\\n"
            "      scope: just_path\\n  http.get:\\n    - host: "
            "registry.modelcontextprotocol.io` to the skill.md frontmatter."
        ),
    },
    "index_drop": {
        "label": "permissions.file.write[.reyn/index/sources.yaml]",
        "fix": (
            "Add `permissions:\\n  file.write:\\n    - path: "
            ".reyn/index/sources.yaml\\n      scope: just_path` to the "
            "skill.md frontmatter."
        ),
    },
}


@dataclass
class ValidationIssue:
    """A single cross-layer consistency issue."""

    severity: str   # "error" | "warning"
    phase: str      # phase name that triggered the issue ("" for skill-level)
    op_kind: str    # op kind involved
    message: str

    def __str__(self) -> str:
        loc = f"phase '{self.phase}'" if self.phase else "skill"
        return f"[{self.severity.upper():7}] {loc}: {self.message}"


@dataclass
class ValidationResult:
    """Aggregate result from validate_skill_dir / validate_skill_object."""

    skill_name: str
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def issues(self) -> list[ValidationIssue]:
        return self.errors + self.warnings

    def __str__(self) -> str:
        lines = [f"Skill '{self.skill_name}':"]
        if not self.issues:
            lines.append("  OK — no cross-layer inconsistencies.")
        else:
            for issue in self.issues:
                lines.append(f"  {issue}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core validation logic — operates on parsed PermissionDecl + Phase objects.
# ---------------------------------------------------------------------------


def _has_file_write(decl, canonical_path: str) -> bool:
    """Return True if *decl* declares ``file.write`` for *canonical_path*."""
    for entry in decl.file_write:
        if isinstance(entry, dict) and entry.get("path") == canonical_path:
            return True
    return False


def _permission_has(decl, op_kind: str) -> bool:
    """Return True if *decl* has an effective declaration for *op_kind*.

    #571 collapse arc Phase 5: ``mcp_install`` / ``index_drop`` no
    longer have dedicated bool axes — their declarations route through
    ``file.write`` for the canonical mutation path (mcp_install also
    needs ``http.get`` for the registry, but the validator stays at
    the file-write granularity for the cross-layer consistency check).
    """
    if op_kind == "shell":
        return bool(decl.shell)
    if op_kind == "mcp":
        return bool(decl.mcp)
    if op_kind == "mcp_install":
        return _has_file_write(decl, ".reyn/mcp.yaml")
    if op_kind == "index_drop":
        return _has_file_write(decl, ".reyn/index/sources.yaml")
    return False  # non-Tier-2/3 op → no declaration needed


def validate_skill_object(skill) -> ValidationResult:
    """Validate a compiled ``Skill`` object for op/permission consistency.

    Parameters
    ----------
    skill:
        A ``reyn.schemas.models.Skill`` instance (already compiled/loaded).

    Returns
    -------
    ValidationResult
        Contains ``errors`` (Tier-2/3 ops used without matching permission)
        and ``warnings`` (permissions declared but never used in any phase).
    """
    result = ValidationResult(skill_name=skill.name)
    decl = skill.permissions

    # Collect all op kinds actually referenced across all phases.
    all_used_ops: set[str] = set()
    for phase_name, phase in skill.phases.items():
        for op_kind in phase.allowed_ops:
            if op_kind not in _TIER23_GATES:
                continue
            all_used_ops.add(op_kind)
            if not _permission_has(decl, op_kind):
                gate = _TIER23_GATES[op_kind]
                result.errors.append(ValidationIssue(
                    severity="error",
                    phase=phase_name,
                    op_kind=op_kind,
                    message=(
                        f"phase uses '{op_kind}' in allowed_ops but "
                        f"skill.{gate['label']} is not declared. "
                        f"Runtime call will raise PermissionError. "
                        f"{gate['fix']}"
                    ),
                ))

    # Dead-declaration check: declared but never referenced.
    for op_kind in _TIER23_GATES:
        if op_kind in all_used_ops:
            continue
        if _permission_has(decl, op_kind):
            gate = _TIER23_GATES[op_kind]
            result.warnings.append(ValidationIssue(
                severity="warning",
                phase="",
                op_kind=op_kind,
                message=(
                    f"skill.{gate['label']} is declared but no phase lists "
                    f"'{op_kind}' in allowed_ops. "
                    f"This is a dead declaration — consider removing it."
                ),
            ))

    return result


# ---------------------------------------------------------------------------
# DSL-level validation (reads .md files without compiling the full skill).
# ---------------------------------------------------------------------------


def _read_frontmatter(path: Path) -> dict:
    """Return the YAML frontmatter dict from a .md file, or {} on error."""
    try:
        import yaml
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}
        end = text.find("\n---", 3)
        if end == -1:
            return {}
        fm_text = text[3:end].strip()
        data = yaml.safe_load(fm_text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def validate_skill_dir(skill_dir: Path) -> ValidationResult:
    """Validate a skill directory (``skill_dir/skill.md`` + ``phases/*.md``).

    Does not compile the full skill — reads frontmatter only.  This is
    intentionally lighter than ``load_dsl_skill`` so it can run cheaply
    as a pre-flight check (e.g. from ``reyn skill validate``).

    Parameters
    ----------
    skill_dir:
        Directory containing ``skill.md`` and ``phases/`` subdirectory.

    Returns
    -------
    ValidationResult
    """
    skill_md = skill_dir / "skill.md"
    if not skill_md.exists():
        result = ValidationResult(skill_name=str(skill_dir))
        result.errors.append(ValidationIssue(
            severity="error",
            phase="",
            op_kind="",
            message=f"skill.md not found at {skill_md}",
        ))
        return result

    skill_fm = _read_frontmatter(skill_md)
    skill_name = skill_fm.get("name") or skill_dir.name
    result = ValidationResult(skill_name=skill_name)

    # Parse permissions from frontmatter.
    from reyn.permissions.permissions import PermissionDecl
    raw_perm = skill_fm.get("permissions") or {}
    if not isinstance(raw_perm, dict):
        raw_perm = {}
    decl = PermissionDecl.from_dict(raw_perm)

    # Collect all Tier-2/3 op kinds used across phases.
    all_used_ops: set[str] = set()
    phases_dir = skill_dir / "phases"
    if phases_dir.exists():
        for phase_file in sorted(phases_dir.glob("*.md")):
            phase_fm = _read_frontmatter(phase_file)
            phase_name = phase_fm.get("name") or phase_file.stem
            allowed_ops_raw = phase_fm.get("allowed_ops")
            if not isinstance(allowed_ops_raw, list):
                continue
            for op_kind in allowed_ops_raw:
                if not isinstance(op_kind, str):
                    continue
                if op_kind not in _TIER23_GATES:
                    continue
                all_used_ops.add(op_kind)
                if not _permission_has(decl, op_kind):
                    gate = _TIER23_GATES[op_kind]
                    result.errors.append(ValidationIssue(
                        severity="error",
                        phase=phase_name,
                        op_kind=op_kind,
                        message=(
                            f"phase uses '{op_kind}' in allowed_ops but "
                            f"skill.{gate['label']} is not declared. "
                            f"Runtime call will raise PermissionError. "
                            f"{gate['fix']}"
                        ),
                    ))

    # Dead-declaration check.
    for op_kind in _TIER23_GATES:
        if op_kind in all_used_ops:
            continue
        if _permission_has(decl, op_kind):
            gate = _TIER23_GATES[op_kind]
            result.warnings.append(ValidationIssue(
                severity="warning",
                phase="",
                op_kind=op_kind,
                message=(
                    f"skill.{gate['label']} is declared but no phase lists "
                    f"'{op_kind}' in allowed_ops. "
                    f"This is a dead declaration — consider removing it."
                ),
            ))

    return result
