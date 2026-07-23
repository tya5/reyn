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

from reyn.llm.model_resolver import resolve_purpose_class  # #1673
from reyn.tools.descriptions import execution as _execution_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/execution.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_SANDBOXED_EXEC_DESCRIPTION = _execution_descriptions.sandboxed_exec.text


# #1339 / sandbox-model completion: the tool exposes ONLY argv (+ timeout). The
# sandbox policy (network / read_paths / write_paths / allow_subprocess /
# env_passthrough) is operator-or-default, resolved onto the OpContext — the LLM
# cannot set it via the tool. (The SandboxedExecIROp keeps those fields for
# phase-authored Control IR; only this tool surface is trimmed.)
_SANDBOXED_EXEC_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": _execution_descriptions.PARAMS["sandboxed_exec"]["argv"].text,
        },
        "timeout_seconds": {
            "type": "integer",
            "description": _execution_descriptions.PARAMS["sandboxed_exec"]["timeout_seconds"].text,
        },
    },
    "required": ["argv"],
}


async def op_context_from_tool_context(ctx: ToolContext) -> Any:
    """Bridge a (args, ctx) ``ToolContext`` into the legacy ``OpContext`` the
    ``op_runtime.sandboxed_exec`` handler (and any other op_runtime handler
    reached this way) expects.

    Used by :func:`_handle` (the ``sandboxed_exec`` tool) — the
    router_state → legacy-OpContext bridge (sandbox_config derivation +
    op_context_factory-or-minimal-synthesis). #3226 Phase 1: the ``shell``
    tool (:mod:`reyn.tools.shell`, #2593), which used to share this bridge,
    was removed outright — it was the sole `/bin/sh -c <str>`
    shell-injection surface in the codebase.
    """
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

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

    # Use op_context_factory if provided, else minimal synthesis.
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
        # Inject derived sandbox_config so the handler uses the configured backend.
        if sandbox_config is not None:
            legacy_ctx = _with_sandbox_config(legacy_ctx, sandbox_config)
        return legacy_ctx

    # Minimal synthesis path (= test sites / narrow callers).
    return OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=PermissionDecl(),
        permission_resolver=ctx.permission_resolver,
        actor="",
        # #1673: real config-aware resolver + "tool" purpose class (was None +
        # literal "standard"). This handler makes no LLM call, but threading the
        # resolver eliminates the resolver=None → litellm-BadRequestError class by
        # construction (uniform with other op handlers that may make LLM calls).
        model=resolve_purpose_class(None, ctx.resolver, "tool"),
        resolver=ctx.resolver,
        subscribers=getattr(ctx.events, "subscribers", []),
        output_language=None,
        sub_state_dir_override=None,
        state_dir_strategy="control_ir",
        mcp_servers={},
        intervention_bus=None,
        current_phase="",
        caller="direct",
        parent_run_id=None,
        sandbox_config=sandbox_config,
    )


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.sandboxed_exec.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx) signature for the sandboxed_exec handler.
    Builds a SandboxedExecIROp from args and a legacy OpContext from
    ToolContext, then delegates to the op_runtime handler.
    """
    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
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
    legacy_ctx = await op_context_from_tool_context(ctx)
    return await handle_sandboxed_exec(op=op, ctx=legacy_ctx)


def _with_sandbox_config(op_ctx: Any, sandbox_config: Any) -> Any:
    """Return a copy of op_ctx with sandbox_config overridden.

    OpContext is a dataclass; we replace() to avoid mutation.
    """
    import dataclasses
    return dataclasses.replace(op_ctx, sandbox_config=sandbox_config)


from reyn.core.offload.canonical import sandboxed_exec_to_canonical  # noqa: E402

SANDBOXED_EXEC = ToolDefinition(
    canonical=sandboxed_exec_to_canonical,
    name="sandboxed_exec",
    description=_SANDBOXED_EXEC_DESCRIPTION,
    parameters=_SANDBOXED_EXEC_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="execution",
    purity="side_effect",
)
