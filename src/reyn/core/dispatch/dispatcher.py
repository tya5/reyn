"""Shared dispatch layer for chat router and op-loop tool invocations.

Wraps any tool invocation with cross-cutting concerns:
  - name validation (against caller's tool catalog)
  - argument validation (against tool's parameters JSON schema)
  - pre/post events (uniform `tool_called` / `tool_returned` / `tool_failed`)
  - error result shape ({status: ok|error, data?, error?: {kind, message}})

Permission checks happen INSIDE the caller-provided `invoker` callable
(via PermissionError); dispatch_tool catches and wraps it uniformly.

Budget / rate-limit recording is a SEPARATE concern handled at the LLM
call boundary (call_llm_tools), not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from reyn.core.dispatch.content_declarations import get_content_fields


class UnknownToolError(Exception):
    """Raised when a name is not in the caller's tool catalog."""


class InvalidArgsError(Exception):
    """Raised when args don't match the tool's parameters schema."""


@dataclass
class DispatchContext:
    """Per-call context passed into dispatch_tool.

    Attributes:
        caller_kind: Always "router" — the chat agent main loop.
            Used in event taxonomy for filtering.
        caller_id: agent_name. Identifies the audit subject.
        chain_id: optional chain id for multi-hop tracing (PR14).
        tool_catalog: dict[str, dict] mapping tool name → tool definition
            ({"function": {"name", "description", "parameters": <json schema>}}).
            Same shape as litellm `tools=` parameter entries.
        events: callable matching events.emit signature
            (def emit(self, event_type: str, **data) -> None).
        chain_id is included as an event field automatically.
        call_id: #4691 Phase B ①(remainder) — the litellm call (LLMToolCallResult
            .call_id, #4725) whose tool_calls this dispatch belongs to. None for
            any caller that does not thread it through (byte-identical to before
            this field existed) — never a minted placeholder. This is the SAME
            key llm_response_received's own call_id carries (#4722); a TUI
            consumer keys a tool row to its parent CALL by this field, not by
            dispatch order (owner ruling B: order is true only while every
            reader reconstructs identically, is one of four faces, and one
            skipped face goes unnoticed — not an invariant to key UI structure
            on).
        completed_response_include_text: #4666 item ③b — mirrors
            ``audit_events.completed_response_include_text`` (②). Governs
            any declared tool field whose content class is "assistant"
            (`reyn.core.dispatch.content_declarations`) — e.g.
            ``ask_user``'s ``question``. Default False matches ②'s own
            config default; every existing construction of this
            dataclass (tests included) is unaffected until it opts in.
        user_input_include_text: #4666 item ③b — mirrors
            ``audit_events.user_input_include_text`` (③). Governs any
            declared tool field whose content class is "user" — e.g.
            ``ask_user``'s ``answer``. Same default-False rationale as
            above.
    """

    caller_kind: Literal["router"]
    caller_id: str
    chain_id: str | None
    tool_catalog: dict[str, dict]
    events: Any  # has .emit(type: str, **data) -> None
    call_id: str | None = None
    completed_response_include_text: bool = False
    user_input_include_text: bool = False


async def dispatch_tool(
    *,
    name: str,
    args: dict,
    ctx: DispatchContext,
    invoker: Callable[[dict], Awaitable[Any]],
) -> dict:
    """Dispatch a tool call with shared cross-cutting concerns.

    Returns a uniform result dict:
        {"status": "ok", "data": <invoker return value>}
        OR
        {"status": "error", "error": {"kind": <str>, "message": <str>}}

    Error kinds:
        - "unknown_tool": name not in ctx.tool_catalog
        - "invalid_args": args fail schema validation
        - "permission_denied": invoker raised PermissionError
        - "exception": invoker raised any other Exception
        - any handler-supplied ``kind`` (see #3450 below) — else "handler_error"

    #3450: a handler that returns NORMALLY (no raise) but its own return value
    declares an error (an ``error`` / ``error_message`` / ``error_kind`` field,
    plain or one level under its own ``{"status": "error", "data": {...}}``
    self-envelope — see :func:`_handler_declared_error`) is promoted to this
    function's OWN ``{"status": "error", ...}`` outer shape too, instead of
    being silently wrapped as ``{"status": "ok", "data": {...error...}}``. Two
    real consumers relied on the outer ``status`` being trustworthy and both
    got fooled by the wrap: the LLM read the outer ``ok`` and never opened
    ``data`` to find the failure (MCP listing failures, #3450's original
    report), and ``routing_decided``'s audit ``outcome`` — which reads THIS
    same outer envelope, not the handler's own return — recorded "success"
    for a failed catalog dispatch (#3429 arc, the second consumer that
    surfaced the identical defect). This is an envelope-level fix, not a
    per-handler one: any handler written in one of the two established
    "declare an error without raising" idioms above is enveloped correctly
    automatically, with no per-handler change required.

    Deliberately excludes MCP's ``isError`` protocol flag: that signal is
    owned by the MCP call/read/get-prompt family's own throw-only contract
    (Session's ``_mcp_call_tool`` / ``_mcp_read_resource`` / ``_mcp_get_prompt``,
    #3447) and its own canonical rendering (``mcp_to_canonical`` et al. in
    ``reyn.core.offload.canonical``); folding ``isError`` in here would
    re-couple this generic envelope check to that family's rendering and
    risk regressing it.

    Events emitted (via ctx.events.emit):
        - tool_called (caller_kind, caller_id, tool, chain_id, call_id, args, args_hash)
        - tool_returned (caller_kind, caller_id, tool, chain_id, call_id, result, args_hash)
            on success.
        - tool_failed (caller_kind, caller_id, tool, chain_id, call_id, error_kind, message)
            on error.

    The invoker callable receives the validated args dict and returns the
    raw result (any JSON-serializable value). PermissionError raised
    inside invoker becomes a "permission_denied" error result.
    """

    # 1. Name validation
    if name not in ctx.tool_catalog:
        # #187 A (deny-message-decision-enabling): suggest the closest catalog tool
        # so the LLM can self-correct a near-miss instead of stalling. The #187
        # dogfood saw the agent guess `source__grep` (from the source__* namespace)
        # → unknown_tool → deterministic stop. A "did you mean <X>?" hint names a
        # real, callable tool from the same catalog.
        import difflib
        _suggestions = difflib.get_close_matches(
            name, list(ctx.tool_catalog), n=1, cutoff=0.6,
        )
        _hint = f" Did you mean {_suggestions[0]!r}?" if _suggestions else ""
        return _error(ctx, name, "unknown_tool",
                      f"Tool {name!r} not in catalog.{_hint}")

    # 2. Argument validation against parameters schema
    schema = (
        ctx.tool_catalog.get(name, {})
        .get("function", {})
        .get("parameters")
    )
    if schema:
        try:
            _validate_args(args, schema)
        except InvalidArgsError as e:
            return _error(ctx, name, "invalid_args", str(e))

    # args_hash is a fingerprint over the FULL, unredacted args — #4666
    # item ③b never touches it (it coexists with args as a correlation
    # key, never a value substitute for it — see _redact_content_fields'
    # own docstring for why redaction must not perturb it).
    args_hash = _compute_args_hash(args)

    # 3. Pre-event: record the tool call.
    ctx.events.emit(
        "tool_called",
        caller_kind=ctx.caller_kind,
        caller_id=ctx.caller_id,
        tool=name,
        chain_id=ctx.chain_id,
        call_id=ctx.call_id,
        args=_redact_content_fields(name, args, ctx),
        args_hash=args_hash,
    )

    # 4. Invoke (with structured error handling)
    try:
        result = await invoker(args)
    except PermissionError as e:
        enriched = _enrich_permission_message(name, str(e))
        ctx.events.emit(
            "tool_failed",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            call_id=ctx.call_id,
            args_hash=args_hash,
            error_kind="permission_denied",
            message=enriched,
        )
        return {"status": "error",
                "error": {"kind": "permission_denied", "message": enriched}}
    except Exception as e:  # noqa: BLE001 — caller errors are normalized
        ctx.events.emit(
            "tool_failed",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            call_id=ctx.call_id,
            args_hash=args_hash,
            error_kind="exception",
            message=f"{type(e).__name__}: {e}",
        )
        return {"status": "error",
                "error": {"kind": "exception",
                          "message": f"{type(e).__name__}: {e}"}}

    # 4b. Envelope correctness (#3450): the invoker returned normally, but its
    # own return value may itself declare an error (see the docstring above
    # and _handler_declared_error's). Promote it to THIS function's outer
    # {"status": "error", ...} shape before the "ok" wrap below, so neither
    # the LLM nor routing_decided ever sees a failure dressed as success.
    if isinstance(result, dict):
        _handler_err = _handler_declared_error(result)
        if _handler_err is not None:
            _err_kind, _err_message = _handler_err
            ctx.events.emit(
                "tool_failed",
                caller_kind=ctx.caller_kind,
                caller_id=ctx.caller_id,
                tool=name,
                chain_id=ctx.chain_id,
                call_id=ctx.call_id,
                args_hash=args_hash,
                error_kind=_err_kind,
                message=_err_message,
            )
            return {"status": "error",
                    "error": {"kind": _err_kind, "message": _err_message}}

    # 5. Post-event: record the result. `result` itself (the return value
    # dispatch_tool hands back to the caller/LLM) is NEVER redacted —
    # only the copy that reaches the audit-event.
    ctx.events.emit(
        "tool_returned",
        caller_kind=ctx.caller_kind,
        caller_id=ctx.caller_id,
        tool=name,
        chain_id=ctx.chain_id,
        call_id=ctx.call_id,
        args_hash=args_hash,
        result=_redact_content_fields(name, result, ctx),
    )
    return {"status": "ok", "data": result}


def _redact_content_fields(name: str, payload: Any, ctx: DispatchContext) -> Any:
    """#4666 item ③b: drop any field *name* declared as conversation
    content (`reyn.core.dispatch.content_declarations`) whose governing
    knob is currently off, from a COPY of *payload* — never mutates
    *payload* itself (the same object the caller returns to the LLM, or
    reuses across ``tool_called``'s ``args`` and the invoker call).

    A tool with no declaration (the overwhelming majority — see the
    registry's own module docstring for the current, single exception)
    is a no-op: `get_content_fields` returns ``{}``, the loop below never
    runs, and *payload* is returned completely unchanged (not even
    shallow-copied) — this function costs nothing for every tool that
    never opted into the mechanism."""
    declared = get_content_fields(name)
    if not declared or not isinstance(payload, dict):
        return payload
    redacted = dict(payload)
    for field, content_class in declared.items():
        if field not in redacted:
            continue
        include = (
            ctx.completed_response_include_text if content_class == "assistant"
            else ctx.user_input_include_text
        )
        if not include:
            redacted.pop(field, None)
    return redacted


def _handler_declared_error(result: dict) -> "tuple[str, str] | None":
    """Return ``(kind, message)`` when *result* — a handler's own non-raising
    return value — declares an error, else ``None`` (#3450).

    Two established "declare a failure without raising" idioms both count,
    covering every handler surveyed across ``src/reyn/tools/*.py`` at the time
    of #3450 (``mcp``/``memory``/``file``/``reyn_repo``/``cron``/``embed``/
    ``pipeline_verbs``/``mcp_verbs``/``skill_verbs``/``plugin_management_verbs``
    and others):

    - a bare error-message field, no self-envelope (``{"error": "..."}`` — the
      MCP ``list_mcp_*`` sentinel is the concrete #3450 report; ``error``/
      ``error_message``/``error_kind`` are the three field names in use). The
      field IS the message; ``kind`` defaults to ``"handler_error"`` (or the
      handler's own ``error_kind`` when present) since there is nothing richer
      to recover from a bare string.
    - the handler already built its OWN dispatch-shaped envelope
      (``{"status": "error", "error": {"kind", "message"}}`` — ``run_pipeline``,
      designed to match this exact vocabulary per #2649 — or
      ``{"status": "error", "data": {"error": ...}}`` — ``mcp_verbs``/
      ``pipeline_verbs``/``skill_verbs``/``plugin_management_verbs``): peel
      ONE level (into ``data``) to reach the message, keeping any ``kind`` the
      handler itself supplied.

    Deliberately does NOT treat a bare ``status == "error"`` (with no
    error-message field reachable) as a failure on its own — a producer whose
    ``status``/``ok`` value is DATA, not a failure signal, is a real, named
    hazard here (``sandboxed_exec``'s ``{"status": "error", "returncode": 2,
    "stdout", "stderr"}`` is a SUCCESSFUL execution reporting a nonzero exit
    code as its own domain data, dispatchable through this same function via
    the unified tool registry). Every genuine error producer surveyed pairs
    its ``status``/self-envelope with one of the three message fields; a bare
    status value never is, by itself, a trigger (mirrors the same tightening
    ``reyn.core.offload.canonical.is_error_result`` already applies at the
    chat-rendering layer, minus its ``isError`` leg — see this module's
    ``dispatch_tool`` docstring for why ``isError`` is out of scope here).
    """
    probe: dict = result
    if result.get("status") == "error":
        data = result.get("data")
        if isinstance(data, dict):
            probe = data
    error_field = probe.get("error")
    if isinstance(error_field, dict) and (error_field.get("kind") or error_field.get("message")):
        kind = str(error_field.get("kind") or "handler_error")
        message = str(error_field.get("message") or "") or "error"
        return kind, message
    for field in ("error_message", "error"):
        value = probe.get(field)
        if value:
            return str(probe.get("error_kind") or "handler_error"), str(value)
    error_kind = probe.get("error_kind")
    if error_kind:
        return str(error_kind), f"error: {error_kind}"
    return None


def _compute_args_hash(args: dict) -> str:
    """Stable fingerprint for args, recorded on the audit events.

    SHA-256 of canonical JSON; safe across Python runs (unlike Python's
    builtin hash() which is randomized).  First 16 hex chars are kept
    (64 bits) — collision risk is acceptable for resume memoization
    within a single run.
    """
    import hashlib
    import json
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — fall back to repr for unhashable args
        canonical = repr(sorted(args.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# Top-level frame fields excluded from the LLM args_hash. ``current_datetime``
# is non-deterministic by design (datetime.now() in the frame) and would
# silently break memo lookup on resume if hashed verbatim.
_LLM_VOLATILE_FRAME_FIELDS: frozenset[str] = frozenset({"current_datetime"})

# Sub-fields excluded when canonicalizing nested objects in the frame. Format
# is "<top_field>.<sub_field>". ``execution.path`` is excluded because the
# runtime restores ``_history`` from ``snap.history`` on resume — but
# ``snap.history`` records phase names while normal operation appends
# transition strings ("draft → review"). The two formats can't be reconciled
# without a snapshot schema extension; until R-D11 lands a proper
# ``transition_history`` field, ``execution.path`` is treated as informational
# (it shows in the LLM context but does not affect memo determinism).
_LLM_VOLATILE_NESTED_FIELDS: frozenset[str] = frozenset({"execution.path"})


def _compute_llm_args_hash(
    *,
    model: str,
    frame: dict,
    prior_attempts: list[dict[str, str]] | None = None,
    rollback_context: dict | None = None,
    system_inputs: dict | None = None,
) -> str:
    """Stable hash for LLM call args. Used as a memoization key on resume.

    Hashes over the inputs that actually drive the LLM's deterministic
    output: model, frame (= the frame model_dump), retry chain, rollback
    context, and system-prompt inputs. Volatile fields (current_datetime,
    execution.path) are stripped before hashing — see
    ``_LLM_VOLATILE_FRAME_FIELDS`` / ``_LLM_VOLATILE_NESTED_FIELDS`` for the
    list and rationale. Without this, every resume would silently miss memo.

    SHA-256 truncated to 16 hex chars, matching ``_compute_args_hash``.
    """
    import hashlib
    import json

    canonical_frame = {}
    for k, v in frame.items():
        if k in _LLM_VOLATILE_FRAME_FIELDS:
            continue
        # Strip nested volatile fields (e.g. "execution.path").
        if isinstance(v, dict):
            cleaned = {
                sub_k: sub_v for sub_k, sub_v in v.items()
                if f"{k}.{sub_k}" not in _LLM_VOLATILE_NESTED_FIELDS
            }
            canonical_frame[k] = cleaned
        else:
            canonical_frame[k] = v

    payload = {
        "model": model,
        "frame": canonical_frame,
        "prior_attempts": prior_attempts or [],
        "rollback_context": rollback_context,
        "system_inputs": system_inputs or {},
    }
    try:
        canonical = json.dumps(
            payload, sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001 — fall back to repr for unhashable values
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── Permission-denied message enrichment ─────────────────────────────────────
# Maps a dispatch tool name → config key the user would set in
# reyn.yaml / reyn.local.yaml to grant the capability. The hint is appended
# to the underlying PermissionError text so the user sees both WHAT was
# denied and HOW to allow it without leaving the chat to read source.
#
# Names cover both router-tool catalog entries (read_file, web_fetch, …)
# and op-loop op kinds (file, web_fetch, …). Unmapped names
# fall back to a generic "see logs / events tab" suffix — better than
# fabricating a config key the user can't actually find.
_PERMISSION_CONFIG_HINTS: dict[str, str] = {
    # File ops — router catalog + op-loop "file" op kind.
    "file": "permissions.file.read / file.write: allow",
    "read_file": "permissions.file.read: allow",
    "write_file": "permissions.file.write: allow",
    "delete_file": "permissions.file.write: allow",
    "list_directory": "permissions.file.read: allow",
    # MCP family.
    "mcp": "permissions.mcp.<server>: allow",
    "call_mcp_tool": "permissions.mcp.<server>: allow",
    "list_mcp_tools": "permissions.mcp.<server>: allow",
    "describe_mcp_tool": "permissions.mcp.<server>: allow",
    "mcp_install": "permissions.mcp_install: allow",
    "mcp_drop_server": "permissions.mcp_drop_server: allow",
    # Web.
    "web_fetch": "permissions.web.fetch: allow",
    "web_search": "permissions.web.search: allow",
    # Index ops.
    "index_drop": "permissions.index_drop: allow",
}


def _enrich_permission_message(tool: str, original: str) -> str:
    """Append an actionable config hint to a PermissionError message.

    The hint points the user at the reyn.yaml / reyn.local.yaml config key
    that grants the capability. Format keeps the original message as the
    prefix (so callers / tests that look for substrings in the underlying
    text continue to work) and adds a single trailing line:

        <original>
        To allow: add `<config-key>` to reyn.local.yaml under permissions:

    Unknown tool names fall back to a generic suffix that points at the
    events tab instead of fabricating a config key.
    """
    hint = _PERMISSION_CONFIG_HINTS.get(tool)
    if hint is None:
        return (
            f"{original}\n"
            f"To allow: see the events tab for the full permission trace."
        )
    return (
        f"{original}\n"
        f"To allow: add `{hint}` to reyn.local.yaml under permissions:"
    )


def _error(ctx: DispatchContext, name: str, kind: str, message: str) -> dict:
    """Emit tool_failed event and return uniform error dict."""
    ctx.events.emit(
        "tool_failed",
        caller_kind=ctx.caller_kind,
        caller_id=ctx.caller_id,
        tool=name,
        chain_id=ctx.chain_id,
        call_id=ctx.call_id,
        error_kind=kind,
        message=message,
    )
    return {"status": "error", "error": {"kind": kind, "message": message}}


def _validate_args(args: dict, schema: dict) -> None:
    """Validate args against a JSON schema (parameters from a tool definition).

    Uses jsonschema.validate. Raises InvalidArgsError on mismatch with a
    short human-readable message.

    Note: jsonschema is already a Reyn dependency (used by artifact_validator).
    """
    try:
        import jsonschema
    except ImportError as e:
        raise InvalidArgsError(f"jsonschema not available: {e}") from e
    try:
        jsonschema.validate(instance=args, schema=schema)
    except jsonschema.ValidationError as e:
        # Compose a short error message highlighting the path
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        raise InvalidArgsError(
            f"args validation failed at {path}: {e.message}"
        ) from e
