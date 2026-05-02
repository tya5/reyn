"""lint kind handler — run the DSL linter against a skill directory."""
from __future__ import annotations
from pathlib import Path
from typing import Literal

from . import register
from .context import OpContext
from reyn.schemas.models import LintIROp


async def handle(op: LintIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    from reyn.compiler.linter import lint_skill_dir

    skill_dir = Path(op.skill_path)
    if not (skill_dir / "skill.md").exists():
        return {
            "kind": "lint",
            "status": "error",
            "skill_path": op.skill_path,
            "passed": False,
            "error_count": 1,
            "warning_count": 0,
            "issues": [f"[ERROR] skill.md not found at '{op.skill_path}'"],
        }
    issues = lint_skill_dir(skill_dir)
    error_count = sum(1 for i in issues if i.severity == "error")
    warning_count = sum(1 for i in issues if i.severity == "warning")
    ctx.events.emit(
        "lint_completed",
        skill_path=op.skill_path,
        error_count=error_count,
        warning_count=warning_count,
    )
    return {
        "kind": "lint",
        "status": "ok",
        "skill_path": op.skill_path,
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": [str(i) for i in issues],
    }


register("lint", handle)
