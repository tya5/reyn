"""Skill-name → filesystem-path resolution.

Used by both the CLI (run/eval/lint) and the runtime (run_skill Control IR op)
to find a skill's directory under reyn/local, reyn/project, or stdlib.

Lives outside the CLI package so the runtime doesn't depend on CLI internals.
"""
from __future__ import annotations

from pathlib import Path


class SkillNotFoundError(FileNotFoundError):
    """Raised when a skill name cannot be resolved to a directory.

    Inherits from FileNotFoundError so callers that catch broad I/O errors
    still see it. Attributes ``name`` and ``checked`` carry the diagnostic
    detail; ``str(exc)`` is suitable for display in error messages.
    """

    def __init__(self, name: str, checked: list[str]):
        self.name = name
        self.checked = checked
        joined = "\n  ".join(checked)
        super().__init__(f"skill '{name}' not found. Looked in:\n  {joined}")


def stdlib_root() -> Path:
    """Absolute path to the bundled stdlib/ tree."""
    return Path(__file__).parent.parent / "stdlib"


def resolve_skill_path(name: str) -> tuple[Path, Path]:
    """Resolve a short skill name to (skill_dir, skill_root).

    Search order: reyn/local → reyn/project → stdlib/skills.

    Raises SkillNotFoundError if no candidate directory contains a skill.md.
    Callers in the CLI layer translate this into a non-zero exit; the
    op-runtime layer lets it propagate so execute_op turns it into an
    op-level error result (status="error") rather than an aborted process.
    """
    sl = stdlib_root()
    candidates: list[tuple[Path, Path]] = [
        (Path("reyn") / "local" / name,    Path("reyn")),
        (Path("reyn") / "project" / name,  Path("reyn")),
        (sl / "skills" / name,             sl),
    ]
    for skill_dir, skill_root in candidates:
        if (skill_dir / "skill.md").exists():
            return skill_dir, skill_root
    raise SkillNotFoundError(name, [str(d / "skill.md") for d, _ in candidates])


def is_stdlib_skill(skill_dir: Path) -> bool:
    """Return True if *skill_dir* lives inside the bundled stdlib tree.

    Used by CLI entry points to decide whether to auto-allow unsafe Python
    preprocessor steps: stdlib skills are shipped by the Reyn team and are
    therefore trusted by construction — the user cannot inject code into them.
    User-provided skills (reyn/local/, reyn/project/) still require the
    --allow-unsafe-python flag.

    The check is purely path-based (no skill name string), keeping the OS
    skill-agnostic in accordance with P7.
    """
    try:
        skill_dir.resolve().relative_to(stdlib_root().resolve())
        return True
    except ValueError:
        return False


def eval_md_path_for(name: str) -> Path:
    """Return the canonical eval.md path for a skill name.

    All skills (stdlib, local, project) write/read eval.md at
    ``.reyn/evals/<name>/eval.md`` — inside the default write zone.

    Validates that the skill exists (SkillNotFoundError if not). The path
    itself is independent of the skill_dir.

    Canonical formula: ``.reyn/evals/<name>/eval.md``.

    This is the authoritative single source. Safe-mode preprocessors
    (``analyze_skill_resolver_pure._derive_eval_output_path`` and
    ``copy_to_work_resolver_pure``) mirror this formula independently due to
    the safe-mode no-reyn-import restriction; consistency is enforced by
    ``test_eval_md_path_consistency``.

    Raises SkillNotFoundError if the skill cannot be found.
    """
    _, _ = resolve_skill_path(name)  # validate skill exists
    return Path(".reyn") / "evals" / name / "eval.md"
