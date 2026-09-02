"""exec ToolDefinition — FP-0034 Phase 2 exec category.

Router-callable capability that exposes the FP-0017
``sandboxed_exec`` op_runtime handler via the universal catalog
(``exec`` qualified name). #3226 Phase 3: the tool itself was
renamed ``sandboxed_exec`` -> ``exec`` (the surviving argv-only exec
primitive, collapsed to the owner-directed name); the op_runtime layer
(``SandboxedExecIROp``, ``OP_KIND_MODEL_MAP["sandboxed_exec"]``, the
``sandboxed_exec_started``/``_completed``/``_cancelled`` audit-events)
is UNCHANGED — only the tool/qualified name + the ``permissions.exec``
key moved.

#4932 (owner ruling, 2026-08-19): the ``exec`` category is ALWAYS visible
to the LLM — it is no longer hidden when no real sandbox backend is
configured (the retired FP-0034 §D14-ext visibility gate). Switching
``sandbox.backend`` to ``"noop"`` for an unrelated reason (#4932's own
repro: probing Keychain reachability) used to make ``exec`` silently
vanish from the catalog with no error and no notice — unpredictable
capability loss the owner ruled against ("UX/predictability outrank
security; security should be opt-in [a real backend], not silently
enforced by hiding a working tool"). ``exec`` still WORKS under
``"noop"``, just without OS-level isolation, so hiding it was never a
security control — the real command still ran, or didn't, on the SAME
permission axis (``gates.router`` + ``exec: allow``) every other
category uses. The catalog enumeration layer
(``universal_catalog._enumerate_category`` / ``_describe_one``) now
DISCLOSES the isolation state in the description text instead
(``universal_catalog.is_exec_isolated`` + ``_EXEC_NO_ISOLATION_NOTICE``)
— never by omission.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.llm.model_resolver import resolve_purpose_class  # #1673
from reyn.tools.descriptions import execution as _execution_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/execution.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_EXEC_DESCRIPTION = _execution_descriptions.exec_.text


# #1339 / sandbox-model completion: the tool exposes argv + timeout only. The
# sandbox policy (network / write_paths / deny_subprocess / env_deny_names —
# vocabulary renamed to deny-lists, #3901 PR-B ④) is operator-or-default,
# resolved onto the OpContext — the LLM cannot set it via the tool. (The
# SandboxedExecIROp type keeps its OWN, older allow_-prefixed fields — #3901
# PR-B deliberately did not rename those; op and policy are different
# vocabularies now, not mirrors of one another — only this tool surface is
# trimmed.) #3962 removed `timeout_seconds` here too (same advertised-but-
# ignored gap #3907 closed for the 5 policy fields, just missed since a
# timeout isn't a permission axis); #3903① (2026-08-11) brought `timeout`
# back deliberately, with a real reader this time (op_runtime/
# sandboxed_exec.py checks it against the operator's own configured
# max_timeout_seconds and applies it — see that module for why this one
# doesn't repeat #3962's mistake).
_EXEC_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": _execution_descriptions.PARAMS["exec"]["argv"].text,
        },
        "timeout": {
            # lead-coder review (#4179): "integer", not "number" — a
            # sub-second override has no meaning here, so the schema
            # itself should say a whole number is expected rather than
            # silently accepting a fraction. The handler still rejects a
            # fractional value explicitly (op_runtime/sandboxed_exec.py) —
            # a model that ignores the schema type hint (not every
            # provider enforces it) still can't reach a silently-truncated
            # timeout.
            "type": "integer",
            "description": _execution_descriptions.PARAMS["exec"]["timeout"].text,
        },
        # #4733: optional — omitted (the default) keeps exec fully
        # synchronous, byte-identical to before this parameter existed.
        # The ONLY declared value is "async" (unlike run_prompt's
        # ["attached", "async"] pair): exec has no "attached"-vs-omitted
        # distinction to make — sync IS what omitting collect already
        # meant, so a redundant "attached" value would just be a second
        # spelling of the default.
        "collect": {
            "type": "string",
            "enum": ["async"],
            "description": _execution_descriptions.PARAMS["exec"]["collect"].text,
        },
    },
    "required": ["argv"],
}


async def op_context_from_tool_context(ctx: ToolContext) -> Any:
    """Bridge a (args, ctx) ``ToolContext`` into the legacy ``OpContext`` the
    ``op_runtime.sandboxed_exec`` handler (and any other op_runtime handler
    reached this way) expects.

    Used by :func:`_handle` (the ``exec`` tool) — the
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
    from reyn.security.sandbox.policy import resolve_sandbox_policy

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
        mcp_servers={},
        intervention_bus=None,
        caller="direct",
        parent_run_id=None,
        sandbox_config=sandbox_config,
        # #3907①: this path had no access to reyn.yaml sandbox.policy at all
        # (no `rs`/op_context_factory here to read it through), so
        # ctx.default_sandbox_policy stayed None — the op_runtime handler's
        # only fallback then is `SandboxedExecIROp`'s own raw op-field
        # defaults (op_runtime/sandboxed_exec.py:69-74), a DIFFERENT
        # computation than every other op path, which resolves through this
        # SAME floor. resolve_sandbox_policy(None) applies the floor (no
        # operator config to merge here — there's genuinely nothing to read
        # it from on this path) rather than leaving this one path uniquely
        # computed from op fields alone.
        default_sandbox_policy=resolve_sandbox_policy(None),
    )


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.sandboxed_exec.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx) signature for the sandboxed_exec op handler.
    Builds a SandboxedExecIROp from args and a legacy OpContext from
    ToolContext, then delegates to the op_runtime handler. The op kind
    stays ``sandboxed_exec`` (#3226 Phase 3 renamed only the tool/
    qualified-name surface, not the Control IR op).
    """
    # #4733: collect="async" dispatches to RouterCallerState.
    # sandboxed_exec_async_fn (bound by RouterLoop, mirrors run_prompt's own
    # collect="async" branch in run_prompt.py) instead of building an
    # OpContext here — the async path registers a chain + spawns a
    # background asyncio.Task on the CALLING session (session_api.
    # run_exec_async), it does not run inline through op_runtime the way
    # the synchronous branch below does.
    if args.get("collect") == "async":
        rs = ctx.router_state
        if rs is None or rs.sandboxed_exec_async_fn is None:
            raise RuntimeError(
                'exec(collect="async") handler requires '
                "ctx.router_state.sandboxed_exec_async_fn to be populated "
                "by the dispatcher (= RouterLoop)."
            )
        return await rs.sandboxed_exec_async_fn(
            argv=args["argv"], timeout_seconds=args.get("timeout"),
        )

    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.schemas.models import SandboxedExecIROp

    # #1339 / sandbox-model completion: the LLM supplies argv (+ optional
    # timeout, #3903①). The op's other policy fields keep their defaults
    # here — the effective sandbox policy is operator-or-default, resolved
    # onto the OpContext (ctx.default_sandbox_policy), which the op_runtime
    # handler applies over the op fields. The LLM cannot set network / fs
    # scope via this tool — timeout is the one axis it CAN extend, bounded
    # by the operator's own configured ceiling (see op_runtime/
    # sandboxed_exec.py for the enforcement).
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=args["argv"],
        timeout_seconds=args.get("timeout"),
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

EXEC = ToolDefinition(
    canonical=sandboxed_exec_to_canonical,
    name="exec",
    # #3429: dispatched DIRECTLY by name. Before the qualified spelling was
    # abolished this tool was reached only through ``invoke_action`` (the
    # ``"__" in name`` arm of ``_invoke_router_tool``), so it never needed the
    # flag; with one name, an advertised action that lacks it lands on the
    # "unhandled tool" safety return. Pinned by
    # ``test_universal_catalog.py::test_every_catalog_action_is_directly_dispatchable``.
    router_dispatched=True,
    description=_EXEC_DESCRIPTION,
    parameters=_EXEC_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="execution",
    purity="side_effect",
)
