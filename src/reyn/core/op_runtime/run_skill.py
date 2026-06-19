"""run_skill op backend — register the op + delegate to reyn.skill.

P3/P4: op registration (``register("run_skill", handle)`` + the ``__init__``
side-effect import) stays in op_runtime; the sub-skill invocation logic lives
in ``reyn.skill.run_skill`` (#1794). This handle is a thin adapter that
forwards the op + context to the skill-package backend.
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import RunSkillIROp
from reyn.skill.run_skill import run as _run

from . import register
from .context import OpContext


async def handle(op: RunSkillIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    return await _run(op, ctx, caller)


register("run_skill", handle)
