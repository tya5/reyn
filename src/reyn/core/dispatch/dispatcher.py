"""Shared dispatch layer for chat router and skill phase tool invocations.

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


class UnknownToolError(Exception):
    """Raised when a name is not in the caller's tool catalog."""


class InvalidArgsError(Exception):
    """Raised when args don't match the tool's parameters schema."""


@dataclass
class DispatchContext:
    """Per-call context passed into dispatch_tool.

    Attributes:
        caller_kind: "router" for chat agent main loop, "skill_phase" for
            skills' op execution. Used in event taxonomy for filtering.
        caller_id: agent_name (router) or f"{skill_name}.{phase_name}"
            (skill_phase). Identifies the audit subject.
        chain_id: optional chain id for multi-hop tracing (PR14).
        tool_catalog: dict[str, dict] mapping tool name → tool definition
            ({"function": {"name", "description", "parameters": <json schema>}}).
            Same shape as litellm `tools=` parameter entries.
        events: callable matching events.emit signature
            (def emit(self, event_type: str, **data) -> None).
        chain_id is included as an event field automatically.
    """

    caller_kind: Literal["router", "skill_phase"]
    caller_id: str
    chain_id: str | None
    tool_catalog: dict[str, dict]
    events: Any  # has .emit(type: str, **data) -> None


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

    Events emitted (via ctx.events.emit):
        - tool_called (caller_kind, caller_id, tool, chain_id, args, args_hash)
            Skipped for ``pure`` op kinds (no side effect to disambiguate)
            and for ``world`` / ``llm`` purity (no side effect ambiguity).
        - tool_returned (caller_kind, caller_id, tool, chain_id, result, args_hash)
            on success.  Skipped for ``pure``.
        - tool_failed (caller_kind, caller_id, tool, chain_id, error_kind, message)
            on error.

    The invoker callable receives the validated args dict and returns the
    raw result (any JSON-serializable value). PermissionError raised
    inside invoker becomes a "permission_denied" error result.
    """
    from reyn.core.op_runtime.registry import OpPurity, get_op_purity

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

    # Determine purity (controls event emission; defaults to side_effect).
    # Note: tool catalog entries from the chat router are not "ops" in the
    # IR-op sense (memory/file/mcp tool entries here are wrappers around
    # multiple op kinds). For chat-router callers, fall back to side_effect.
    purity = get_op_purity(name) if ctx.caller_kind == "skill_phase" else OpPurity.side_effect
    args_hash = _compute_args_hash(args)

    # 3. Pre-event (skip for pure / world / llm — no side-effect ambiguity)
    if purity in (OpPurity.side_effect, OpPurity.external):
        ctx.events.emit(
            "tool_called",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            args=args,
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
            args_hash=args_hash,
            error_kind="exception",
            message=f"{type(e).__name__}: {e}",
        )
        return {"status": "error",
                "error": {"kind": "exception",
                          "message": f"{type(e).__name__}: {e}"}}

    # 5. Post-event with result (skipped for ``pure``: re-execution is safe
    #    and cheap, no need to record).
    if purity != OpPurity.pure:
        ctx.events.emit(
            "tool_returned",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            args_hash=args_hash,
            result=result,
        )
    return {"status": "ok", "data": result}


def _compute_args_hash(args: dict) -> str:
    """Stable fingerprint for args, recorded on the audit events.

    SHA-256 of canonical JSON; safe across Python runs (unlike Python's
    builtin hash() which is randomized).  First 16 hex chars are kept
    (64 bits) — collision risk is acceptable for resume memoization
    within a single skill run.
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
# without a SkillSnapshot schema extension; until R-D11 lands a proper
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
# and skill_phase op kinds (file, shell, web_fetch, …). Unmapped names
# fall back to a generic "see logs / events tab" suffix — better than
# fabricating a config key the user can't actually find.
_PERMISSION_CONFIG_HINTS: dict[str, str] = {
    # File ops — router catalog + skill_phase "file" op kind.
    "file": "permissions.file.read / file.write: allow",
    "read_file": "permissions.file.read: allow",
    "write_file": "permissions.file.write: allow",
    "delete_file": "permissions.file.write: allow",
    "list_directory": "permissions.file.read: allow",
    # Shell.
    "shell": "permissions.shell: allow",
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
    "drop_source": "permissions.index_drop: allow",
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
