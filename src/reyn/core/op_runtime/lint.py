"""lint kind handler — run the DSL linter against a skill directory."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from reyn.schemas.models import LintIROp

from . import register
from .context import OpContext


async def handle(op: LintIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    from reyn.core.compiler.linter import lint_skill_dir

    # B49 W3-S6 fix (2026-05-22): accept the qualified action name format
    # returned by ``list_actions(category=['skill'])`` (= ``skill__<name>``)
    # so the LLM can pass discovery output verbatim, without inferring a
    # prefix strip. Bare names and workspace-relative paths continue to
    # work via the fallbacks below.
    raw_skill_path = op.skill_path
    if raw_skill_path.startswith("skill__"):
        raw_skill_path = raw_skill_path[len("skill__"):]

    skill_dir = Path(raw_skill_path)
    if not (skill_dir / "skill.md").exists():
        # Fallback: treat raw_skill_path as a short skill name and resolve
        # it via the standard search path (reyn/local → reyn/project →
        # stdlib). This covers both the qualified-name path (= prefix
        # stripped above) and bare names passed by phase-side callers.
        try:
            from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path

            resolved_dir, _ = resolve_skill_path(raw_skill_path)
            skill_dir = resolved_dir
        except Exception:
            pass  # SkillNotFoundError or ImportError → fall through to error below

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
