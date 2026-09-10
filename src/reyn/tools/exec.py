"""exec ToolDefinition — FP-0034 Phase 2 exec category.

Router-callable capability that exposes the FP-0017
``sandboxed_exec`` op_runtime handler via the universal catalog
(``exec`` qualified name). #3226 Phase 3: the tool itself was
renamed ``sandboxed_exec`` -> ``exec`` (the surviving exec primitive,
collapsed to the owner-directed name); the op_runtime layer
(``SandboxedExecIROp``, ``OP_KIND_MODEL_MAP["sandboxed_exec"]``, the
``sandboxed_exec_started``/``_completed``/``_cancelled`` audit-events)
is UNCHANGED — only the tool/qualified name + the ``permissions.exec``
key moved.

#5838 段6: the LLM-facing schema (``_EXEC_PARAMETERS``) now exposes a
second, mutually-exclusive request shape alongside ``argv`` — ``cmd``
(a shell command-line string, parsed for POLICY via
``security.exec_plan``/``security.exec_plan_policy`` and then run via
the sandbox's shell with the string UNCHANGED, never a reconstruction
from the parsed plan; see ``core/op_runtime/sandboxed_exec.py``'s own
module docstring for the full owner-ruling rationale). Exactly one of
argv/cmd is enforced as a TOOL ERROR by ``_handle`` below (never a
JSON Schema ``oneOf``/``anyOf``, and never a raw ``KeyError``) —
``argv`` stays the primary, unchanged path for every existing caller;
``cmd`` has no ``collect="async"`` leg in this stage (a deliberate
scope boundary, not an oversight — see ``_handle``'s own comment).

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


# #1339 / sandbox-model completion: the tool exposes argv + timeout +
# network only. The sandbox policy (write_paths / deny_subprocess /
# env_deny_names — vocabulary renamed to deny-lists, #3901 PR-B ④) is
# operator-or-default, resolved onto the OpContext — the LLM cannot set
# those axes via the tool. (The SandboxedExecIROp type keeps its OWN,
# older allow_-prefixed fields — #3901 PR-B deliberately did not rename
# those; op and policy are different vocabularies now, not mirrors of one
# another — only this tool surface is trimmed.) #3962 removed
# `timeout_seconds` here too (same advertised-but-ignored gap #3907
# closed for the 5 policy fields, just missed since a timeout isn't a
# permission axis); #3903① (2026-08-11) brought `timeout` back
# deliberately, with a real reader this time (op_runtime/
# sandboxed_exec.py checks it against the operator's own configured
# max_timeout_seconds and applies it — see that module for why this one
# doesn't repeat #3962's mistake). #5825① (2026-09-06) brought `network`
# back the same way — a REQUEST, not a grant, gated by a real reader
# (op_runtime/sandboxed_exec.py's own seam calls require_network) rather
# than #3907's advertised-but-ignored shape.
_EXEC_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": _execution_descriptions.PARAMS["exec"]["argv"].text,
        },
        # #5838 段6: exactly one of argv / cmd must be given. NOT expressed
        # via JSON Schema oneOf/anyOf (architect ruling, #5838 issue
        # thread) — a oneOf/anyOf XOR is not guaranteed to be enforced by
        # every LLM-facing schema consumer, and a missed enforcement would
        # fall straight back into the KeyError this stage exists to
        # prevent. The XOR check lives in `_handle` below instead,
        # returning a tool error (never an exception) that names which
        # parameter to use. `SandboxedExecIROp`'s own model_validator
        # re-checks the SAME invariant at the op level (defense in depth —
        # it is only reached once `_handle` has already read args["argv"]/
        # args["cmd"] without a KeyError, so it can never be the FIRST
        # thing to catch a violation coming from this tool).
        "cmd": {
            "type": "string",
            "description": _execution_descriptions.PARAMS["exec"]["cmd"].text,
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
        # #5825①: a REQUEST, not a grant — "declaration is intent, the
        # prompt is the grant" (require_http_get's own framing, applied
        # here). Omitted/false changes nothing (byte-identical to before
        # this parameter existed). true against a policy that already has
        # network closed triggers require_network (config pre-approval, a
        # persisted grant, or an interactive ask) before the process
        # spawns — see op_runtime/sandboxed_exec.py's own seam.
        "network": {
            "type": "boolean",
            "description": _execution_descriptions.PARAMS["exec"]["network"].text,
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
    # #5838 段6: `argv` is no longer required here — `cmd` alone is a
    # valid call now too. The XOR itself is enforced in `_handle`, not
    # expressible as `required` (there is no "exactly one of A, B"
    # `required` shape in JSON Schema) — see the `cmd` property comment
    # above for why this stays a tool error, not a schema-level oneOf.
    "required": [],
}


async def op_context_from_tool_context(ctx: ToolContext) -> Any:
    """Bridge a (args, ctx) ``ToolContext`` into the legacy ``OpContext`` the
    ``op_runtime.sandboxed_exec`` handler (and any other op_runtime handler
    reached this way) expects.

    Used by :func:`_handle` (the ``exec`` tool) — the
    router_state → legacy-OpContext bridge (sandbox backend-instance
    resolution + op_context_factory-or-minimal-synthesis). #3226 Phase 1:
    the ``shell`` tool (:mod:`reyn.tools.shell`, #2593), which used to
    share this bridge, was removed outright — it was the sole
    `/bin/sh -c <str>` shell-injection surface in the codebase.

    #5820 (owner-hit, security, silent): this bridge used to derive a
    policy-less ``SandboxConfig(backend=...)`` from ``RouterCallerState.
    sandbox_backend`` (a NAME string) and OVERWRITE ``legacy_ctx.
    sandbox_config`` wholesale with it — clobbering the operator's real
    declared policy `legacy_ctx.sandbox_config` already carried (from
    ``op_context_factory()``), which ``describe_session``'s ``write_scope``
    field then read as "nothing declared", falsely, for every session with
    a real (non-``None``) sandbox backend. Fixed by giving the backend
    NAME its own existing seam instead: resolved to a real
    :class:`~reyn.security.sandbox.backend.SandboxBackend` INSTANCE here,
    it goes onto ``OpContext.sandbox_backend`` (that field's own docstring
    already documents "an injected instance wins over name-based auto-
    selection" as its purpose) — ``sandbox_config`` is never rewritten
    post-construction anywhere in this bridge any more.
    """
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    # #5820 (owner-hit, security, silent): derive a resolved SandboxBackend
    # INSTANCE from RouterCallerState.sandbox_backend (a NAME string — a
    # DIFFERENT thing from OpContext.sandbox_backend below, same name, see
    # that field's own docstring for the disambiguation) when available.
    # This used to build a policy-less SandboxConfig(backend=backend) and
    # OVERWRITE legacy_ctx.sandbox_config with it wholesale (_with_sandbox_
    # config, now removed) — legacy_ctx.sandbox_config, when op_context_
    # factory is used below, already carries the REAL operator-declared
    # config (policy included); that whole-object overwrite silently
    # clobbered the declared policy with this synthesized, policy-less
    # stand-in, which describe_session's write_scope field then read as
    # "nothing declared" — false, for every session with a real (non-None)
    # sandbox_backend. sandbox_config is never touched post-construction
    # any more (test_repo/test_5820_... pins this structurally); the
    # resolved backend instance goes onto OpContext.sandbox_backend
    # instead — the field's OWN docstring already names "an injected
    # instance wins over name-based auto-selection" as its seam, this NAME-
    # resolved instance is simply an earlier-resolved member of that same
    # seam (see that field's own docstring, corrected in this PR, for why
    # its 2 real readers never distinguish "genuinely stateful" from
    # "resolved early").
    resolved_backend = None
    rs = ctx.router_state
    if rs is not None:
        backend_name = getattr(rs, "sandbox_backend", None)
        if backend_name is not None:
            from reyn.config import SandboxConfig
            from reyn.security.sandbox import get_default_backend
            try:
                resolved_backend = get_default_backend(SandboxConfig(backend=backend_name))
            except ValueError:
                resolved_backend = None

    # Use op_context_factory if provided, else minimal synthesis.
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
        if resolved_backend is not None:
            import dataclasses
            legacy_ctx = dataclasses.replace(legacy_ctx, sandbox_backend=resolved_backend)
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
        # #5820: this path has no real config to populate sandbox_config
        # with (see the comment on default_sandbox_policy below) — None,
        # never the synthesized backend-only object the pre-#5820 code
        # built here (that object's own None .policy is exactly what made
        # describe_session's write_scope lie for the OTHER branch above;
        # kept out of this field entirely now).
        sandbox_config=None,
        sandbox_backend=resolved_backend,
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
        #
        # #5818 (owner-hit, security, lead-coder ruling): `mode="compat"` is
        # explicit here, never read from any config object — this
        # construction point (reached only when `rs is None` or `rs.op_
        # context_factory is None`, i.e. no real router/host context at
        # all) never reaches reyn.yaml (there is no operator config
        # available here at all, #5820's own fix left `sandbox_config`
        # above `None` rather than a synthesized stand-in). The
        # construction point that DOES have the real config is
        # `router_op_context.py`'s own `build_router_op_context` (fixed by
        # #5818) — reached here too, first, via `rs.op_context_factory`
        # above, whenever a real host exists. This path claiming `strict`
        # would assert an operator setting it structurally cannot see — the
        # setting would silently stay unenforced while `default_sandbox_
        # policy` reported "compat" as if that were the operator's real,
        # deliberate choice.
        default_sandbox_policy=resolve_sandbox_policy(None, mode="compat"),
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
    # #5838 段6: exactly one of argv / cmd, checked HERE (before either
    # branch below reads args["argv"] directly) -- relaxing the schema's
    # `required` to let `cmd` alone through (this tool's own `_EXEC_
    # PARAMETERS` comment) means a `cmd`-only call reaches this function
    # with no "argv" key at all, and a bare `args["argv"]` read below
    # would be a raw KeyError -- an exception the LLM cannot act on,
    # never reaching `SandboxedExecIROp`'s own XOR `model_validator` (that
    # validator is reached only once an op has ALREADY been constructed,
    # i.e. only after a bare `args["argv"]` read has already either
    # succeeded or raised). `bool(argv)` mirrors the op's own validator
    # exactly (`argv=[]` -- the schema has no "omitted" for an array type
    # -- is treated as "not given", same as `SandboxedExecIROp._exactly_
    # one_of_argv_or_cmd`), so this check and that one never disagree.
    given_argv = bool(args.get("argv"))
    given_cmd = args.get("cmd") is not None
    if given_argv == given_cmd:  # both or neither
        return {
            "kind": "sandboxed_exec",
            "status": "error",
            "error": (
                "exec requires exactly one of argv / cmd — "
                f"{'both were given' if given_argv else 'neither was given'}. "
                "Use argv (a list of command + arguments) for the plain "
                "form, or cmd (a shell command-line string, e.g. pipes/"
                "redirects) for shell syntax — not both, not neither."
            ),
        }

    # #5838 段6: `cmd` has no async leg -- `run_exec_async` (session_api.
    # py) has signature `argv: list[str]`, no `cmd` parameter, a
    # deliberate scope boundary (not an oversight; wiring `cmd` through
    # the async path is explicitly OUT OF SCOPE for this stage -- file a
    # separate stage if/when it's wanted). Rejecting here, before the
    # collect="async" branch below ever reads `args["argv"]`, turns what
    # would otherwise be a raw KeyError into a tool error the LLM can act
    # on.
    if given_cmd and args.get("collect") == "async":
        return {
            "kind": "sandboxed_exec",
            "status": "error",
            "error": (
                "cmd is sync-only; use argv for async execution "
                '(collect="async" has no cmd support in this release).'
            ),
        }

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
            network=bool(args.get("network", False)),
        )

    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.schemas.models import SandboxedExecIROp

    # #1339 / sandbox-model completion: the LLM supplies argv OR cmd (#5838
    # 段6, checked exactly-one above) + optional timeout (#3903①). The
    # op's other policy fields keep their defaults here — the effective
    # sandbox policy is operator-or-default, resolved onto the OpContext
    # (ctx.default_sandbox_policy), which the op_runtime handler applies
    # over the op fields. The LLM cannot set fs scope via this tool —
    # timeout (#3903①) and network (#5825①) are the two axes it CAN
    # request, both bounded by a real gate (see op_runtime/
    # sandboxed_exec.py for timeout's ceiling check and its own #5825 ①
    # seam for network's ask-or-deny). `argv=args.get("argv") or []`: the
    # op's own default is `[]` (Field(default_factory=list)) for a
    # cmd-only call — passing the schema's omitted value through as `[]`
    # rather than `None` matches that default exactly, never reaching the
    # op's `_exactly_one_of_argv_or_cmd` validator with a mismatched shape.
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=args.get("argv") or [],
        cmd=args.get("cmd"),
        timeout_seconds=args.get("timeout"),
        network=bool(args.get("network", False)),
    )
    legacy_ctx = await op_context_from_tool_context(ctx)
    return await handle_sandboxed_exec(op=op, ctx=legacy_ctx)


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
