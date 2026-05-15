"""skill_resolve op — resolve a skill name to its on-disk skill.md path.

P7-compliant: name is the only skill-specific string in/out; the OS reads
the canonical resolution chain (reyn/local/ → reyn/project/ → stdlib/)
without privileging any of them. The op exists to move fs-touching path
resolution out of stdlib python steps (Class D refactor in
R-PURE-MODE-REDEFINE) — those steps then become pure dict transforms over
the op output and can declare mode: safe.

OpPurity: world (read-only fs metadata walk; no writes, no external API).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from reyn.schemas.models import SkillResolveIROp
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root

from . import register
from .context import OpContext


def _categorize_source(skill_dir: Path) -> str | None:
    """Return "project" | "local" | "stdlib" based on path components.

    Checks against canonical stdlib root first (absolute comparison so the
    check is robust regardless of CWD). Falls back to inspecting whether the
    path passes through ``reyn/local`` or ``reyn/project`` directory segments.
    """
    try:
        skill_dir.resolve().relative_to(stdlib_root().resolve())
        return "stdlib"
    except ValueError:
        pass

    parts = skill_dir.parts
    # Walk parts to detect reyn/local or reyn/project in sequence
    for i, part in enumerate(parts):
        if part == "reyn" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt == "local":
                return "local"
            if nxt == "project":
                return "project"

    return None


async def handle(
    op: SkillResolveIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Resolve a skill name via the canonical resolution chain.

    Returns a result dict describing the resolved skill location.
    Never raises — resolution failure yields resolved=False with null fields
    and emits skill_resolve_completed(resolved=False).
    """
    name = op.name

    try:
        skill_dir, _skill_root = resolve_skill_path(name)
    except (SkillNotFoundError, FileNotFoundError):
        ctx.events.emit("skill_resolve_completed", name=name, resolved=False, source=None)
        return {
            "name": name,
            "resolved": False,
            "skill_md_path": None,
            "source": None,
            "skill_dir": None,
        }

    source = _categorize_source(skill_dir)
    skill_md_path = skill_dir / "skill.md"

    ctx.events.emit(
        "skill_resolve_completed",
        name=name,
        resolved=True,
        source=source,
    )
    return {
        "name": name,
        "resolved": True,
        "skill_md_path": str(skill_md_path),
        "source": source,
        "skill_dir": str(skill_dir),
    }


register("skill_resolve", handle)
