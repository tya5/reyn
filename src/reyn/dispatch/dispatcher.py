"""Shared dispatch layer for chat router and skill phase tool invocations.

Wraps any tool invocation with cross-cutting concerns:
  - name validation (against caller's tool catalog)
  - argument validation (against tool's parameters JSON schema)
  - pre/post events (uniform `tool_called` / `tool_returned` / `tool_failed`)
  - error result shape ({status: ok|error, data?, error?: {kind, message}})

Permission checks happen INSIDE the caller-provided `invoker` callable
(via PermissionError); dispatch_tool catches and wraps it uniformly.

Budget / rate-limit recording is a SEPARATE concern handled at the LLM
call boundary (call_llm / call_llm_tools), not here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

    The ``result`` and ``args_hash`` fields are part of the skill resume
    transactional-replay design: on resume, these events let the runtime
    use the recorded result instead of re-executing a side-effecting op.

    The invoker callable receives the validated args dict and returns the
    raw result (any JSON-serializable value). PermissionError raised
    inside invoker becomes a "permission_denied" error result.
    """
    from reyn.op_runtime.registry import OpPurity, get_op_purity

    # 1. Name validation
    if name not in ctx.tool_catalog:
        return _error(ctx, name, "unknown_tool",
                      f"Tool {name!r} not in catalog")

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
        ctx.events.emit(
            "tool_failed",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            args_hash=args_hash,
            error_kind="permission_denied",
            message=str(e),
        )
        return {"status": "error",
                "error": {"kind": "permission_denied", "message": str(e)}}
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
    """Stable hash for args.  Used as a memoization key on resume.

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
