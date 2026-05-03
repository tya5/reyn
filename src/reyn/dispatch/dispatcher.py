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

    Skill-resume fields (PR-skill-resume part A):
        state_log: optional WAL for crash recovery. When set together with
            ``skill_run_id`` and ``caller_kind == 'skill_phase'``, dispatch
            emits ``step_started``/``step_completed``/``step_failed``
            events to the WAL alongside the audit-log events. Decoupled
            from audit emission: step events drive forward-replay resume,
            audit events drive forensics. Same OpPurity gating applies.
        skill_run_id: identifier of the currently-executing skill run.
            Required for step-event emission; ignored if ``state_log`` is
            None.
        phase: name of the phase whose ops are being dispatched. Embedded
            in step events so resume scans can scope to a phase boundary.

    Skill-resume memoization field (PR-skill-resume part D3b):
        resume_plan: optional ``ResumePlan`` from ``SkillResumeAnalyzer``.
            When set, dispatch_tool consults ``resume_plan.committed_steps``
            before invoking — a matching (op_invocation_id + args_hash)
            triggers memoization (return the recorded result without
            invoking). Drives forward-replay resume; ``args_hash``
            mismatch falls through to fresh execution (drift detection).
            ``None`` means normal execution (no memoization), which is
            the default for fresh starts.
    """

    caller_kind: Literal["router", "skill_phase"]
    caller_id: str
    chain_id: str | None
    tool_catalog: dict[str, dict]
    events: Any  # has .emit(type: str, **data) -> None
    # Skill-resume fields (optional; only meaningful for skill_phase caller).
    state_log: Any = None       # has async .append(kind, **fields) -> int
    skill_run_id: str | None = None
    phase: str | None = None
    # Resume memoization (PR-skill-resume D3b). Optional ResumePlan with
    # the committed_steps list dispatch_tool consults before invoking.
    resume_plan: Any = None     # has .committed_steps: list[CommittedStep]


async def dispatch_tool(
    *,
    name: str,
    args: dict,
    ctx: DispatchContext,
    invoker: Callable[[dict], Awaitable[Any]],
    op_invocation_id: str | None = None,
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

    # 2.5. Resume memoization (PR-skill-resume D3b-1c).
    # If a ResumePlan is wired in and we find a CommittedStep matching the
    # current call (op_invocation_id + phase + args_hash), reproduce the
    # recorded outcome without invoking. This prevents:
    #   - duplicate side effects on resume (the canonical resume concern)
    #   - wasted LLM costs (when llm calls memoize through the same path)
    # args_hash mismatch falls through deliberately — the LLM may have
    # emitted a structurally different op shape this resume, in which
    # case the recorded result no longer applies (drift detection).
    memo = _lookup_memoized_step(
        ctx.resume_plan, op_invocation_id, ctx.phase, args_hash,
    )
    if memo is not None:
        ctx.events.emit(
            "step_memoized",
            caller_kind=ctx.caller_kind,
            caller_id=ctx.caller_id,
            tool=name,
            chain_id=ctx.chain_id,
            op_invocation_id=op_invocation_id,
            args_hash=args_hash,
            recorded_seq=memo.seq,
        )
        if memo.error_kind is not None:
            return {"status": "error",
                    "error": {"kind": memo.error_kind,
                              "message": memo.error_message or ""}}
        return {"status": "ok", "data": memo.result}

    # Step-event emission gate (PR-skill-resume part A).
    # Step events go to the WAL (durable, drives forward-replay resume);
    # audit events go to ctx.events (forensics). Only emit step events for
    # skill-phase callers with a state_log + skill_run_id wired in.
    emit_step = (
        ctx.caller_kind == "skill_phase"
        and ctx.state_log is not None
        and ctx.skill_run_id is not None
    )

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
        if emit_step:
            await _wal_step_started(
                ctx, name, args, args_hash, op_invocation_id,
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
        if emit_step and purity != OpPurity.pure:
            await _wal_step_failed(
                ctx, name, args_hash, op_invocation_id,
                "permission_denied", str(e),
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
        if emit_step and purity != OpPurity.pure:
            await _wal_step_failed(
                ctx, name, args_hash, op_invocation_id,
                "exception", f"{type(e).__name__}: {e}",
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
        if emit_step:
            await _wal_step_completed(
                ctx, name, args_hash, op_invocation_id, result,
            )
    return {"status": "ok", "data": result}


async def _wal_step_started(
    ctx: DispatchContext,
    op_kind: str,
    args: dict,
    args_hash: str,
    op_invocation_id: str | None,
) -> None:
    """Append a ``step_started`` WAL entry. Defensive: log + swallow on failure.

    Truncation correctness depends on WAL durability, not on every step
    event landing — a missing step_started just means resume must treat the
    op as unknown (and prompt). Swallowing here protects the hot path.
    """
    try:
        await ctx.state_log.append(
            "step_started",
            run_id=ctx.skill_run_id,
            phase=ctx.phase,
            op_invocation_id=op_invocation_id,
            op_kind=op_kind,
            args=args,
            args_hash=args_hash,
        )
    except Exception as e:  # noqa: BLE001 — never fail the dispatch
        import logging
        logging.getLogger(__name__).warning(
            "WAL step_started emission failed (run=%s op=%s): %s",
            ctx.skill_run_id, op_kind, e,
        )


async def _wal_step_completed(
    ctx: DispatchContext,
    op_kind: str,
    args_hash: str,
    op_invocation_id: str | None,
    result: Any,
) -> None:
    """Append a ``step_completed`` WAL entry with the recorded result."""
    try:
        await ctx.state_log.append(
            "step_completed",
            run_id=ctx.skill_run_id,
            phase=ctx.phase,
            op_invocation_id=op_invocation_id,
            op_kind=op_kind,
            args_hash=args_hash,
            result=result,
        )
    except Exception as e:  # noqa: BLE001 — never fail the dispatch
        import logging
        logging.getLogger(__name__).warning(
            "WAL step_completed emission failed (run=%s op=%s): %s",
            ctx.skill_run_id, op_kind, e,
        )


async def _wal_step_failed(
    ctx: DispatchContext,
    op_kind: str,
    args_hash: str,
    op_invocation_id: str | None,
    error_kind: str,
    message: str,
) -> None:
    """Append a ``step_failed`` WAL entry."""
    try:
        await ctx.state_log.append(
            "step_failed",
            run_id=ctx.skill_run_id,
            phase=ctx.phase,
            op_invocation_id=op_invocation_id,
            op_kind=op_kind,
            args_hash=args_hash,
            error_kind=error_kind,
            message=message,
        )
    except Exception as e:  # noqa: BLE001 — never fail the dispatch
        import logging
        logging.getLogger(__name__).warning(
            "WAL step_failed emission failed (run=%s op=%s): %s",
            ctx.skill_run_id, op_kind, e,
        )


def _lookup_memoized_step(
    resume_plan: Any,
    op_invocation_id: str | None,
    phase: str | None,
    args_hash: str,
) -> Any:
    """Find a CommittedStep matching the current call, or return None.

    Match criteria (all must hold for a hit):
      - op_invocation_id equal (phase-relative ID)
      - phase equal (same phase visit context)
      - args_hash equal (drift detection — different args = re-execute)

    The last-write-wins semantic: if multiple CommittedSteps match
    (theoretically possible if the WAL has duplicates from a buggy
    earlier run), the *most recently appended* one wins (highest seq).
    This handles the edge case of a botched truncation that left two
    completions for the same step.

    ``resume_plan`` is typed Any to keep the dispatch module decoupled
    from skill module imports — duck-typed access via ``committed_steps``.
    """
    if resume_plan is None or op_invocation_id is None:
        return None
    committed = getattr(resume_plan, "committed_steps", None)
    if not committed:
        return None
    best = None
    for step in committed:
        if (step.op_invocation_id == op_invocation_id
                and step.phase == (phase or "")
                and step.args_hash == args_hash):
            if best is None or step.seq > best.seq:
                best = step
    return best


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


# Top-level frame fields excluded from the LLM args_hash. ``current_datetime``
# is non-deterministic by design (datetime.now() in ContextFrame) and would
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

    Hashes over the inputs that actually drive ``call_llm`` deterministic
    output: model, frame (= ContextFrame.model_dump), retry chain, rollback
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
