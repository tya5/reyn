"""lint ToolDefinition — Wave 1 migration (ADR-0026 M3).

Router-accessible capability: gates.router="allow", gates.phase="allow".
Exposed as ``validation__lint`` via invoke_action so the router can lint
a skill directly from a user request ("lint the foo skill").

The existing handler in src/reyn/op_runtime/lint.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx, caller) signature and the new (args, ctx) signature.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.llm.model_resolver import resolve_purpose_class  # #1673
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_LINT_DESCRIPTION = (
    "Run the DSL linter on a skill and return structured issue results. "
    "skill_path: identifier of the skill to lint. Accepts the qualified "
    "skill action name (= 'skill__<name>'), the bare skill name, or a "
    "workspace-relative path to the skill directory. Skill names are resolved via the "
    "standard search path (reyn/local → reyn/project → stdlib). "
    "Returns passed, error_count, warning_count, and issues."
)

_LINT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill_path": {
            "type": "string",
            "description": (
                'qualified action name like "skill__<name>" '
                "(returned by list_actions), the bare skill name, "
                "or a workspace-relative directory path. NOT a "
                "slash-style path under \"skill/\""
            ),
        },
    },
    "required": ["skill_path"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.lint.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature. Once M3 Wave 1 succeeds,
    the body of lint.handle may be inlined here in M4 cleanup.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.lint import handle as handle_lint
    from reyn.schemas.models import LintIROp
    from reyn.security.permissions.permissions import PermissionDecl

    # Build a transient LintIROp from args (= reuse Pydantic
    # validation that the existing op handler expects).
    op = LintIROp(
        kind="lint",
        skill_path=args["skill_path"],
    )

    # Build a legacy OpContext from the new ToolContext.
    # Propagate the active phase's PermissionDecl + intervention_bus
    # via phase_state.op_context (FP-0008 Tool→OpContext bridge fix
    # 2026-05-28). Lint itself is read-only / structural-analysis only,
    # but uniform bridge wiring avoids future class bugs if lint ever
    # gains permission-gated paths.
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    legacy_ctx = OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
        skill=None,
        # #1673: real resolver + "tool" purpose class (was None + literal
        # "standard") — eliminates the resolver=None → litellm-BadRequestError class
        # by construction (uniform with invoke_skill; this handler makes no LLM call).
        model=resolve_purpose_class(None, ctx.resolver, "tool"),
        resolver=ctx.resolver,
        subscribers=getattr(ctx.events, "subscribers", []),
        output_language=None,
        max_phase_visits=25,
        sub_state_dir_override=None,
        state_dir_strategy="control_ir",
        mcp_servers={},
        mcp_clients={},
        intervention_bus=getattr(phase_op_ctx, "intervention_bus", None),
        current_phase="",
        caller="direct",
        parent_skill_run_id=None,
    )

    return await handle_lint(op=op, ctx=legacy_ctx, caller="control_ir")


LINT = ToolDefinition(
    name="lint",
    description=_LINT_DESCRIPTION,
    parameters=_LINT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="validation",
    purity="read_only",   # lint reads + reports, no workspace mutation
)
