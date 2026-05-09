"""shell ToolDefinition — ADR-0026 M3 Wave 1.

Phase-only capability (gates.router="deny", gates.phase="allow").
Shell is a security boundary: the router never exposes it.
The phase-side `shell` op kind is the only consumer.

The existing handler in src/reyn/op_runtime/shell.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx, caller) signature and the new (args, ctx) signature.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# Description derived from ShellIROp docstring + control-ir.md ## shell section.
# Shell is phase-only; no legacy router ToolSpec to match byte-for-byte.
_SHELL_DESCRIPTION = (
    "Execute a shell command and return stdout, stderr, and exit code. "
    "Off by default — requires --allow-shell and project permission. "
    "cmd: shell command string. "
    "timeout: max seconds to wait (default 120)."
)

# Parameters JSON schema matching ShellIROp fields (cmd: str, timeout: int = 120).
_SHELL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cmd": {"type": "string"},
        "timeout": {"type": "integer"},
    },
    "required": ["cmd"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.shell.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature. Once M3/M4 stabilises,
    the body of handle may be inlined here.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.shell import handle as handle_shell
    from reyn.schemas.models import ShellIROp
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    # Build a transient ShellIROp from args (= reuse Pydantic validation
    # that the existing op handler expects).
    op = ShellIROp(
        kind="shell",
        cmd=args["cmd"],
        timeout=int(args.get("timeout", 120)),
    )

    # Build a legacy OpContext from the new ToolContext.
    # shell_allowed=True because gate enforcement (Layer 1) has already
    # passed by the time the handler is invoked. The permission_resolver
    # on ToolContext, if present, will still be consulted by handle_shell
    # for the per-call Layer 3 check (require_shell). When permission_resolver
    # is None we fall back to shell_allowed=True (gate passed = execution allowed).
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
        shell_allowed=True,
        mcp_servers={},
        mcp_clients={},
        intervention_bus=getattr(ctx.phase_state, "intervention_bus", None),
        current_phase="",
        caller="direct",
        parent_skill_run_id=None,
    )

    return await handle_shell(op=op, ctx=legacy_ctx, caller="control_ir")


SHELL = ToolDefinition(
    name="shell",
    description=_SHELL_DESCRIPTION,
    parameters=_SHELL_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),  # phase-only — security boundary
    handler=_handle,
    category="execution",
    purity="side_effect",
)
