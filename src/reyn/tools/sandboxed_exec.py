"""sandboxed_exec ToolDefinition — FP-0034 Phase 2 exec category.

Router-and-phase callable capability that exposes the FP-0017
``sandboxed_exec`` op_runtime handler via the universal catalog
(``exec__sandboxed_exec`` qualified name).

D14-ext visibility gating: the ``exec`` category is only shown to the
LLM when a real sandbox backend is configured (= not "noop" / not None).
The ToolDefinition is always in the registry; the catalog enumeration
layer (``universal_catalog._enumerate_category``) performs the gate
check using ``RouterCallerState.sandbox_backend``.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_SANDBOXED_EXEC_DESCRIPTION = (
    "Execute a command in a sandboxed environment (FP-0017). The sandbox "
    "policy (network access + filesystem scope) is the OPERATOR's, resolved "
    "by the OS — it is not chosen here. "
    "argv: command and arguments (argv[0] is the executable). "
    "timeout_seconds: wall-clock time limit in seconds (default 60)."
)


# #1339 / sandbox-model completion: the tool exposes ONLY argv (+ timeout). The
# sandbox policy (network / read_paths / write_paths / allow_subprocess /
# env_passthrough) is operator-or-default, resolved onto the OpContext — the LLM
# cannot set it via the tool. (The SandboxedExecIROp keeps those fields for
# skill-authored Control IR; only this tool surface is trimmed.)
_SANDBOXED_EXEC_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Command and arguments; argv[0] is the executable.",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Wall-clock time limit in seconds (default 60).",
        },
    },
    "required": ["argv"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.sandboxed_exec.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature for the sandboxed_exec handler.
    Builds a SandboxedExecIROp from args and a legacy OpContext from
    ToolContext, then delegates to the op_runtime handler.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import SandboxedExecIROp

    # #1339 / sandbox-model completion: the LLM supplies only argv (+ timeout).
    # The op's policy fields keep their defaults here — the effective sandbox
    # policy is operator-or-default, resolved onto the OpContext
    # (ctx.default_sandbox_policy), which the op_runtime handler applies over the
    # op fields. The LLM cannot set network / fs scope via this tool.
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=args["argv"],
        timeout_seconds=int(args.get("timeout_seconds", 60)),
    )

    # Derive sandbox_config from RouterCallerState.sandbox_backend when
    # available, otherwise fall back to None (= op_runtime auto-detects).
    sandbox_config = None
    rs = ctx.router_state
    if rs is not None:
        backend = getattr(rs, "sandbox_backend", None)
        if backend is not None:
            from reyn.config import SandboxConfig
            try:
                sandbox_config = SandboxConfig(backend=backend)
            except ValueError:
                sandbox_config = None

    # Phase-side: prefer the pre-built OpContext when available.
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    if phase_op_ctx is not None:
        return await handle_sandboxed_exec(
            op=op, ctx=phase_op_ctx, caller="control_ir",
        )

    # Router-side: use op_context_factory if provided, else minimal synthesis.
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
        # Inject derived sandbox_config so the handler uses the configured backend.
        if sandbox_config is not None:
            legacy_ctx = _with_sandbox_config(legacy_ctx, sandbox_config)
        return await handle_sandboxed_exec(
            op=op, ctx=legacy_ctx, caller="control_ir",
        )

    # Minimal synthesis path (= test sites / narrow callers).
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
        sandbox_config=sandbox_config,
    )
    return await handle_sandboxed_exec(op=op, ctx=legacy_ctx, caller="control_ir")


def _with_sandbox_config(op_ctx: Any, sandbox_config: Any) -> Any:
    """Return a copy of op_ctx with sandbox_config overridden.

    OpContext is a dataclass; we replace() to avoid mutation.
    """
    import dataclasses
    return dataclasses.replace(op_ctx, sandbox_config=sandbox_config)


SANDBOXED_EXEC = ToolDefinition(
    name="sandboxed_exec",
    description=_SANDBOXED_EXEC_DESCRIPTION,
    parameters=_SANDBOXED_EXEC_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="execution",
    purity="side_effect",
)
