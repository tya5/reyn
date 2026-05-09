"""invoke_skill ToolDefinition — naming canonicalization to router-side
fine-grained name (ADR-0026 Open Q #6).

Phase-side `run_skill` op kind continues to work via OP_KIND_MODEL_MAP
backward-compat (= ADR-0026 Open Q #7 hybrid recommendation: alias
preserved + deprecation in a later release).

Note: this ToolSpec has dynamic per-call schema (= enum injection from
available_skills) on router side. The static parameters JSON schema in
this ToolDefinition represents the shape WITHOUT the per-call enum;
router-side code that injects the enum (= router_tools.py inline pattern)
can stay inline for this capability since the registry render produces
the static base shape only. M4 may surface a per-call render override.

Per-call enum enrichment:
  On the router side, build_tools() wraps the `name` field with
  ``{"type": "string", "enum": skill_names}`` when skill_names is
  non-empty (see _invoke_skill_name_schema in router_tools.py). This
  enrichment is caller-context-specific and cannot be expressed in a
  static ToolDefinition.parameters without a per-call render hook.
  render_for_router() on this ToolDefinition therefore produces the
  static base shape (name is "type": "string" with no enum). The router
  continues to inject the enum inline (= existing router_tools.py logic
  preserved until M4 introduces a per-call render override mechanism).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# Description must be byte-identical to the router_tools.py invoke_skill
# ToolSpec.description (= lines 369-377 in router_tools.py). Copied verbatim.
_INVOKE_SKILL_DESCRIPTION = (
    "Run a skill from the registered list. "
    "The 'name' parameter MUST be one of the skills "
    "listed in the system prompt's \"Available skills\" "
    "section, used verbatim (no dots, no slashes, "
    "no namespace prefixes). "
    "Use list_skills' input_fields hint to construct "
    "the correct input, or call describe_skill for full "
    "schema details. Do not guess input field names."
)

# Static base parameters JSON schema — WITHOUT the per-call dynamic enum.
# The `name` field is {"type": "string"} here; on the router side,
# build_tools() enriches this to {"type": "string", "enum": [...]}
# via _invoke_skill_name_schema. The per-call enrichment is a router-side
# concern not handled by render_for_router() on this ToolDefinition.
# See module docstring for rationale.
_INVOKE_SKILL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Skill name — choose exactly one from "
                "the enum (verbatim, no dots or slashes)."
            ),
        },
        "input": {
            "type": "object",
            "description": (
                "Skill input artifact: "
                "{type: <artifact_type>, data: {...}}"
            ),
        },
    },
    "required": ["name", "input"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.run_skill.handle.

    Bridges between the unified (args, ctx) signature and the existing
    (op, ctx, caller) signature. Constructs a RunSkillIROp from args
    and builds a legacy OpContext from the ToolContext, mirroring the
    pattern established in web_search.py (M2 POC).

    Phase-side `run_skill` op kind continues to be dispatched through
    OP_KIND_MODEL_MAP["run_skill"] = RunSkillIROp — backward-compat alias
    per ADR-0026 Open Q #7. This handler is used when the capability is
    invoked via the unified registry (M3+); the OP_KIND_MODEL_MAP path
    remains active for phase-side control_ir until M4 wires the alias.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.run_skill import handle as handle_run_skill
    from reyn.schemas.models import RunSkillIROp
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    # Build a transient RunSkillIROp from args (= reuse existing handler
    # expectation; model/workspace/output_language fields default).
    op = RunSkillIROp(
        kind="run_skill",
        skill=args["name"],
        input=args["input"],
        model=args.get("model", ""),
        workspace=args.get("workspace", "isolated"),
        output_language=args.get("output_language", None),
    )

    # Build a legacy OpContext from the new ToolContext.
    # PermissionDecl() with empty defaults is safe here because the
    # run_skill handler derives its permission checks from the sub-skill's
    # own permission_resolver (ctx.permission_resolver is forwarded).
    # This mirrors the web_search adapter shim pattern (M2 POC).
    legacy_ctx = OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=PermissionDecl(),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
        skill=None,
        model="standard",
        resolver=None,
        subscribers=getattr(ctx.events, "subscribers", []),
        output_language=None,
        max_phase_visits=25,
        sub_state_dir_override=None,
        state_dir_strategy="control_ir",
        shell_allowed=False,
        mcp_servers={},
        mcp_clients={},
        intervention_bus=None,
        current_phase="",
        caller="direct",
        parent_skill_run_id=None,
    )

    return await handle_run_skill(op=op, ctx=legacy_ctx, caller="control_ir")


INVOKE_SKILL = ToolDefinition(
    name="invoke_skill",
    description=_INVOKE_SKILL_DESCRIPTION,
    parameters=_INVOKE_SKILL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="invocation",
    purity="side_effect",
)
