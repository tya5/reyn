"""describe_session ToolDefinition (#5012-A) — read-only session introspection.

Router-callable LLM entry point for the model's own write-scope / position /
auth-status facts. No arguments; the handler delegates to
``op_runtime.describe_session``, which assembles the 3-field report from the
real ``OpContext`` (populated via the standard ``build_legacy_op_context``
bridge, same shape every other delegating tool uses).

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import discovery as _discovery_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_DESCRIBE_SESSION_DESCRIPTION = _discovery_descriptions.describe_session.text

_DESCRIBE_SESSION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}


async def _handle_describe_session(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the describe_session op via op_runtime.

    No-arg tool: `args` is accepted to match the shared ToolHandler protocol
    but unused (there is nothing for the model to supply)."""
    del args
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import DescribeSessionIROp
    from reyn.tools.op_context_bridge import build_legacy_op_context

    op = DescribeSessionIROp(kind="describe_session")
    legacy_ctx = build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx)


from reyn.core.offload.canonical import describe_session_to_canonical  # noqa: E402

DESCRIBE_SESSION = ToolDefinition(
    canonical=describe_session_to_canonical,
    name="describe_session",
    router_dispatched=True,
    description=_DESCRIBE_SESSION_DESCRIPTION,
    parameters=_DESCRIBE_SESSION_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_describe_session,
    category="discovery",
    purity="read_only",
)
