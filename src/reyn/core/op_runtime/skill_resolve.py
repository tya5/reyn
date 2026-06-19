"""skill_resolve op backend — register the op + delegate to reyn.skill.

P3/P4: op registration (``register("skill_resolve", handle)`` + the
``__init__`` side-effect import) stays in op_runtime; the resolution logic
lives in ``reyn.skill.skill_resolve`` (#1794). This handle is a thin adapter
that forwards the op + context to the skill-package backend.

OpPurity: world (read-only fs metadata walk; no writes, no external API).
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import SkillResolveIROp
from reyn.skill.skill_resolve import resolve as _resolve

from . import register
from .context import OpContext


async def handle(
    op: SkillResolveIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    return await _resolve(op, ctx, caller)


register("skill_resolve", handle)
