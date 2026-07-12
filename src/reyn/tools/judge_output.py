"""judge_output ToolDefinition (proposal 0060 F3b) — chat + pipeline invocation surface.

The ``judge_output`` op handler (``op_runtime/judge_output.py``) and IR op model
(``JudgeOutputIROp``) already existed (FP-0007 Component D) but had NO invocation
surface: no ``ToolDefinition`` meant ``registry.lookup("judge_output")`` returned
``None``, so a pipeline ``tool: {name: judge_output}`` step raised "does not
resolve to a registered tool" — the op was reachable only from the legacy
phase-graph runtime, never from a pipeline (0060 F3b flagship-pipeline build:
confirmed by actually building + running the flagship and hitting this gap).

This module is the additive fix, mirroring the ``present`` precedent
(``tools/present.py`` / #2692): a single ``ToolDefinition`` registered in
``get_default_registry()`` opens the invocation surface (pipeline via
bare-name lookup, chat via ``gates.router="allow"``). The handler builds the
real ``JudgeOutputIROp`` and dispatches through ``execute_op`` with the real
``OpContext`` (the ``compact``/``present`` precedent), so the existing op
handler's LLM-scoring logic and P6 audit emission are UNCHANGED — this tool
adds no new behavior, only reachability.

``target``/``data_inline`` are the same XOR pair the op model itself
validates (0060 F3b addition — see ``JudgeOutputIROp`` docstring for why
``data_inline`` exists: a pipeline `agent`-step's output lives in the
pipeline's own ``ctx`` store, never in ``ctx.workspace.artifacts``, so
``target`` alone was unreachable from a pipeline).

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import judge_output_to_canonical
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_JUDGE_OUTPUT_DESCRIPTION = (
    "Score a value against a rubric using an LLM judge — returns a 0.0-1.0 "
    "score and a pass/fail flag against `threshold`. Give the value to score "
    "via `data_inline` (a value you already have, e.g. a prior step's "
    "output) — `target` is a legacy dot-path form for the phase-graph "
    "runtime only; supply exactly one. `rubric` is your own scoring "
    "criteria in plain language; the OS never interprets it. Use this to "
    "gate a self-authored or generated artifact before promoting/shipping "
    "it (0060 J-D amortization: auto-improvement promotion is judge-gated)."
)

_JUDGE_OUTPUT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": "Legacy dot-path into the phase-graph workspace artifact (e.g. \"artifact.data.summary\"). XOR data_inline.",
        },
        "data_inline": {
            "description": "The value to score, already in hand (e.g. a pipeline agent-step's output). XOR target.",
        },
        "rubric": {
            "type": "string",
            "description": "Your scoring criteria in plain language. The OS never interprets this content.",
        },
        "threshold": {
            "type": "number",
            "description": "Passing score in [0.0, 1.0]. Defaults to 0.8.",
        },
        "on_fail": {
            "type": "string",
            "enum": ["transition", "abort", "continue"],
            "description": "Recorded in the result for the caller to act on; the op itself does not branch on it.",
        },
        "model": {
            "type": "string",
            "description": "Optional model class override. Defaults to the judge purpose class.",
        },
    },
    "required": ["rubric"],
}


async def _handle_judge_output(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the judge_output op via op_runtime.

    Builds a JudgeOutputIROp from the tool args and calls the registered
    judge_output handler with the real OpContext from ctx.router_state's
    factory (the compact.py/present.py precedent), or a minimal context
    otherwise. An arg-level XOR violation (both/neither of target/data_inline)
    is caught as a clean error, not a crash.
    """
    from pydantic import ValidationError

    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import JudgeOutputIROp
    from reyn.security.permissions.permissions import PermissionDecl

    try:
        op = JudgeOutputIROp(
            kind="judge_output",
            target=args.get("target"),
            data_inline=args.get("data_inline"),
            rubric=args["rubric"],
            threshold=float(args.get("threshold", 0.8)),
            on_fail=args.get("on_fail", "transition"),
            model=args.get("model"),
        )
    except ValidationError as exc:
        return {"kind": "judge_output", "status": "error", "ok": False, "error": str(exc)}

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context: no phase-graph workspace artifacts wired (only
        # relevant to the legacy target= path; data_inline needs none of it).
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            actor="",
            subscribers=getattr(ctx.events, "subscribers", []),
            resolver=ctx.resolver,
        )

    return await execute_op(op, legacy_ctx)


JUDGE_OUTPUT = ToolDefinition(
    canonical=judge_output_to_canonical,
    name="judge_output",
    router_dispatched=True,
    description=_JUDGE_OUTPUT_DESCRIPTION,
    parameters=_JUDGE_OUTPUT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_judge_output,
    category="evaluation",
    purity="side_effect",
    # proposal 0060 D5d: a structured pointer at the full judge_output spec —
    # this op's target/data_inline XOR + rubric/threshold/on_fail semantics
    # exceed what the tool description can carry, so a spec-bearing pointer
    # completes the 動線 (the same rail present/render_template carry).
    doc_ref="docs/reference/runtime/control-ir.md#judge_output",
)
