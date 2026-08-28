"""``emit_behavior_anomaly_verdict`` ToolDefinition (#5221).

The ONLY producer of the ``behavior_anomaly_judged`` audit-event kind. A
`tool` step in the behavioral-anomaly-detector pipeline
(``src/reyn/data/pipelines/behavior_anomaly.yaml``) calls this — with args
built from ``ctx.verdict`` (the judge ``agent`` step's schema-constrained
JSON reply, via ``!expr``) — right after the judge answers, so the audit
trail always carries a durable record of what the judge said.

**``gates.router="deny"`` is load-bearing, not incidental**: this tool is
NEVER offered to a live agent's own tool menu. A `tool` step's own dispatch
(``reyn.core.pipeline.executor._run_tool_step`` → the pipeline driver's
``tool_dispatch``) resolves a NAME the operator wrote into the registered
DSL file, not a name an LLM chose from a menu — `gates.router` governs
router-menu ADVERTISEMENT only (see ``reyn.tools.registry.ToolRegistry.
by_router_gate``), so it has zero effect on whether a pipeline `tool` step
can call this. Denying the router gate means a live agent (the very thing
under observation) can never call this tool DIRECTLY to manufacture a
``verdict="clean"`` record for itself — the only path that can ever produce
one is the deterministic pipeline the operator registered.

**Asymmetric trust, enforced in the type, not just prose**: ``verdict`` is
validated against exactly the closed ``{"clean", "suspicious"}`` vocabulary
— there is no way to pass a "verified_clean" or any other value a caller
might invent to claim a stronger guarantee than "the judge did not flag
it". See the pipeline's own module docstring for the full asymmetric-trust
statement this event's ``verdict`` field is read under.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import make_status_text_mapper
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_VERDICT_VALUES = ("clean", "suspicious")

_EMIT_BEHAVIOR_ANOMALY_VERDICT_DESCRIPTION = (
    "Internal: record a behavioral-anomaly judge verdict onto the audit "
    "trail. Never offered to a live agent — called only from the "
    "behavior_anomaly pipeline's own tool step."
)

_EMIT_BEHAVIOR_ANOMALY_VERDICT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(_VERDICT_VALUES),
            "description": "The judge's verdict — 'clean' (not flagged) or 'suspicious' (flagged).",
        },
        "chain_id": {
            "type": "string",
            "description": "The turn's chain_id this verdict belongs to (joins to turn_settled/turn_completed).",
        },
        "anomalous_op_count": {
            "type": "integer",
            "description": "The sensitive-op tally that triggered escalation to the judge.",
        },
    },
    "required": ["verdict", "chain_id", "anomalous_op_count"],
}


async def _handle_emit_behavior_anomaly_verdict(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    verdict = args.get("verdict")
    if verdict not in _VERDICT_VALUES:
        return {
            "status": "error",
            "error": f"verdict must be one of {_VERDICT_VALUES!r}, got {verdict!r}",
        }
    chain_id = str(args.get("chain_id") or "")
    try:
        anomalous_op_count = int(args.get("anomalous_op_count", 0))
    except (TypeError, ValueError):
        return {"status": "error", "error": "anomalous_op_count must be an integer"}

    ctx.events.emit(
        "behavior_anomaly_judged",
        verdict=verdict,
        chain_id=chain_id,
        anomalous_op_count=anomalous_op_count,
    )
    return {"status": "ok", "verdict": verdict, "chain_id": chain_id}


def _render_emit_behavior_anomaly_verdict(result: dict) -> str:
    return f"Recorded behavior-anomaly verdict '{result.get('verdict', '')}'."


emit_behavior_anomaly_verdict_to_canonical = make_status_text_mapper(
    render=_render_emit_behavior_anomaly_verdict, meta_keys=("verdict", "chain_id"),
)

EMIT_BEHAVIOR_ANOMALY_VERDICT = ToolDefinition(
    canonical=emit_behavior_anomaly_verdict_to_canonical,
    name="emit_behavior_anomaly_verdict",
    description=_EMIT_BEHAVIOR_ANOMALY_VERDICT_DESCRIPTION,
    parameters=_EMIT_BEHAVIOR_ANOMALY_VERDICT_PARAMETERS,
    gates=ToolGates(router="deny"),
    handler=_handle_emit_behavior_anomaly_verdict,
    category="observability",
    purity="side_effect",
)
