"""invoke_skill ToolDefinition — naming canonicalization to router-side
fine-grained name (ADR-0026 Open Q #6).

#1240 Wave 2b: the coarse-name RUN_SKILL_OP (kind="run_skill") is dropped.
INVOKE_SKILL (kind="invoke_skill", gates.phase="allow") is now the canonical
phase-advertised ToolDefinition.  Phase-side dispatch works via the
(A)-alias in _PHASE_TOOL_NAME_ALIAS: available_ops() advertises "invoke_skill",
the parse boundary (op_loop + json-mode) rewrites it to "run_skill" before
ControlIROp validation, and execute_op falls through to op_runtime/run_skill.py.

Dispatch status (post-#1240 Wave 2b):
  - schema_enricher (_enrich_router_schema) is wired into the INVOKE_SKILL
    ToolDefinition.  render_for_router(state=...) injects the `name` enum
    from RouterCallerState.available_skills per-call.
  - Phase-side: available_ops() advertises kind="invoke_skill"; the alias
    maps it to kind="run_skill" at parse; execute_op dispatches to
    op_runtime/run_skill.py:handle via the legacy execute_op fallback.
  - Router-side INVOKE_SKILL handler dispatch still goes through the
    legacy adapter in router_loop.py rather than ``invoke_tool``.  Moving
    that path to the unified registry would require surfacing
    ``run_skill_fn`` on RouterCallerState — a follow-up if router-side
    unification becomes load-bearing.

Per-call enum enrichment:
  The `name` field in _INVOKE_SKILL_PARAMETERS is a plain string (no enum).
  _enrich_router_schema injects the enum at render time when
  RouterCallerState.available_skills is non-empty. When empty, the name
  field remains a plain string (consistent with the prior inline logic
  in router_tools.py that omitted invoke_skill entirely when no skills
  are registered — the omission guard is preserved in build_tools()).
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

if TYPE_CHECKING:
    from reyn.tools.types import RouterCallerState


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


def _enrich_router_schema(rendered: dict, state: "RouterCallerState") -> dict:
    """Inject `name` enum from available_skills (= dynamic per-session data).

    Matches the prior inline literal in router_tools.py: when there's at
    least one skill, the name field gets an enum constraint. When there
    are zero skills, the schema falls back to plain string (no enum).

    Returns a NEW dict — does not mutate the input.
    """
    available_skills = state.available_skills or []
    skill_names = [s["name"] for s in available_skills if "name" in s]
    new = copy.deepcopy(rendered)
    name_prop = new["function"]["parameters"]["properties"].get("name")
    if name_prop is None:
        return new  # defensive: schema is missing the name field
    if skill_names:
        name_prop["enum"] = skill_names
    else:
        name_prop.pop("enum", None)
    return new


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for invoke_skill.

    Router path (= production, ADR-0026 Phase 3.5-B-light): delegate to
    ``ctx.router_state.run_skill_fn`` which is RouterLoop-bound to
    ``host.run_skill_awaitable`` with chain_id pre-applied.  This path
    preserves multi-hop chain identity (= PR14 pending_chain semantics)
    that the op_runtime fallback path drops.  Defense Layer B
    (= skill name validation against ``available_skills``) is also
    applied here so a hallucinated skill name is rejected before the
    sub-skill task spawns.

    FP-0012 chat-mode preference: when ``rs.spawn_skill_fn`` is
    populated (= chat-mode RouterLoop, ChatSession host), prefer the
    non-blocking spawn path. The handler returns the spawn-ack
    ``{status: "spawned", run_id, chain_id, note}`` immediately and the
    background task delivers completion via the ``skill_completed``
    inbox kind. Plan-mode RouterLoops leave ``spawn_skill_fn=None`` so
    plan steps keep blocking semantics (= step's LLM sees the actual
    skill result and can synthesize the next step's input).

    Fallback (= phase-side dispatch / test sites): build a transient
    RunSkillIROp + minimal OpContext and call op_runtime.run_skill.handle
    directly.  PR14 chain semantics do not apply phase-side, so the
    chain_id loss is OK in that path.
    """
    rs = ctx.router_state

    # Router path — delegate via the populated callable (chain_id bound)
    if rs is not None and (
        rs.spawn_skill_fn is not None or rs.run_skill_fn is not None
    ):
        # Defense Layer B: validate skill name against available_skills
        # so hallucinated names raise before spawning. Mirrors the
        # explicit check in the legacy RouterLoop branch (now removed).
        skill_name = args["name"]
        if rs.available_skills:
            available = {s["name"] for s in rs.available_skills if "name" in s}
            if available and skill_name not in available:
                raise ValueError(
                    f"skill {skill_name!r} not found; "
                    f"available: {sorted(available)}"
                )
        # FP-0012: prefer non-blocking spawn when chat-mode binding is
        # active. Plan-mode falls through to run_skill_fn (blocking).
        if rs.spawn_skill_fn is not None:
            return await rs.spawn_skill_fn(
                skill=skill_name,
                input=args["input"],
            )
        return await rs.run_skill_fn(
            skill=skill_name,
            input=args["input"],
        )

    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.run_skill import handle as handle_run_skill
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import RunSkillIROp

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
    # Propagate the active phase's PermissionDecl + intervention_bus
    # via phase_state.op_context (FP-0008 Tool→OpContext bridge fix
    # 2026-05-28).
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
        model="standard",
        resolver=None,
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

    return await handle_run_skill(op=op, ctx=legacy_ctx, caller="control_ir")


INVOKE_SKILL = ToolDefinition(
    name="invoke_skill",
    description=_INVOKE_SKILL_DESCRIPTION,
    parameters=_INVOKE_SKILL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="invocation",
    purity="side_effect",
    schema_enricher=_enrich_router_schema,
)

# #1240 Wave 2b: RUN_SKILL_OP (the coarse phase-side ToolDefinition under the
# name "run_skill") is DROPPED.  Phase Control IR now advertises the chat name
# "invoke_skill" via available_ops() (ControlIROpSpec with kind="invoke_skill"),
# which aliases to op kind "run_skill" at the parse boundary.  Dispatch falls to
# the legacy execute_op path (op_runtime/run_skill.py register("run_skill")).
# allowed_ops=[run_skill] continues to match the invoke_skill spec via
# _PHASE_TOOL_NAME_ALIAS in runtime.build_frame.
# KEPT: INVOKE_SKILL (router+phase, gates.phase="allow") is the canonical
# phase-advertised ToolDefinition.  The invoke_skill handler (phase path) builds
# RunSkillIROp and delegates to op_runtime.run_skill.handle directly.
