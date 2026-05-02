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
    """Resolve a short skill name to (skill_dir, dsl_root).

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
    for skill_dir, dsl_root in candidates:
        if (skill_dir / "skill.md").exists():
            return skill_dir, dsl_root
    raise SkillNotFoundError(name, [str(d / "skill.md") for d, _ in candidates])
