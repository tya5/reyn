"""Op runtime — shared backend for executing ControlIROp.

Both `PreprocessorExecutor` (static frontend) and `ControlIRExecutor`
(dynamic frontend) dispatch through `execute_op` here. The op kind catalog
and per-op semantics live in this package; the frontends decide *when*
to invoke an op and *where* to bind its result.

`ask_user` is the only op that cannot be invoked from preprocessor
(static execution can't pause for user input). All other ops, including
side-effect ones (file.write/edit/delete, shell), are callable from both
frontends — gating is provided by the existing PermissionResolver, which
is call-site agnostic.
"""
from __future__ import annotations

import time
import uuid
from typing import Literal

from reyn.schemas.models import ControlIROp

from .context import OpContext
from .registry import OpPurity, get_op_purity
from .result import OpDenied, OpResult, OpSkipped


class OpDispatchError(RuntimeError):
    """Raised when execute_op is called with an unsupported op kind."""


_PREPROCESSOR_FORBIDDEN_KINDS = frozenset({"ask_user"})

# Max cell-width for ``args_summary`` / ``result_summary`` event payloads.
# Bounded so the event log doesn't blow up on a 50 MB file content arg or
# similar pathological payload; consumers (= TUI ToolCallRow) further
# truncate to terminal width.
_OP_SUMMARY_MAX_CHARS = 120

# Fields that are typically multi-KB and not useful in a one-line summary.
# Replaced with a ``<N chars>`` placeholder when present so the summary
# stays readable and the event log stays small.
_OP_ARG_BULKY_FIELDS = frozenset({
    "content", "new_string", "old_string", "body", "preview",
})


def _summarize_op_args(op: ControlIROp) -> str:
    """One-line repr of the op's user-visible args for event display.

    Pydantic ``model_dump`` then formats key=value pairs, excluding bulky
    free-form fields (= ``content`` etc) which are summarised as
    ``<N chars>``. Bounded to ``_OP_SUMMARY_MAX_CHARS`` cells with ellipsis.
    Best-effort: returns empty string on any error so the event still
    fires.
    """
    try:
        data = op.model_dump(exclude_none=True)
    except Exception:
        return ""
    data.pop("kind", None)
    parts: list[str] = []
    for key, value in data.items():
        if key in _OP_ARG_BULKY_FIELDS and isinstance(value, str) and len(value) > 24:
            parts.append(f"{key}=<{len(value)} chars>")
            continue
        s = str(value)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{key}={s}")
    summary = ", ".join(parts)
    if len(summary) > _OP_SUMMARY_MAX_CHARS:
        summary = summary[: _OP_SUMMARY_MAX_CHARS - 1] + "…"
    return summary


def _summarize_op_result(result: dict) -> str:
    """One-line repr of the op result dict for the ``tool_completed`` event.

    Excludes bulky body fields (= same set as args). Bounded to
    ``_OP_SUMMARY_MAX_CHARS``. Returns empty string when result has
    nothing presentable.
    """
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    for key, value in result.items():
        if key in _OP_ARG_BULKY_FIELDS and isinstance(value, str) and len(value) > 24:
            parts.append(f"{key}=<{len(value)} chars>")
            continue
        s = str(value)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{key}={s}")
    summary = ", ".join(parts)
    if len(summary) > _OP_SUMMARY_MAX_CHARS:
        summary = summary[: _OP_SUMMARY_MAX_CHARS - 1] + "…"
    return summary


async def execute_op(
    op: ControlIROp,
    ctx: OpContext,
    *,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Dispatch a ControlIROp to its handler and return a result dict.

    Returns a JSON-serializable dict in the same shape callers have
    historically appended to `control_ir_results`. Errors are captured
    in the dict (status="error" / "denied" / "skipped"); this function
    never raises for op-level failures.

    Static-execution constraints:
      - `ask_user` is rejected when caller=="preprocessor" because static
        enrichment cannot pause for user input.

    Permission checks happen here (single point) — callers do not need to
    invoke `require_*` themselves.
    """
    if caller == "preprocessor" and op.kind in _PREPROCESSOR_FORBIDDEN_KINDS:
        return {
            "kind": op.kind,
            "status": "denied",
            "error": f"op kind '{op.kind}' cannot be invoked from preprocessor",
        }

    handler = _HANDLERS.get(op.kind)
    if handler is None:
        ctx.events.emit("control_ir_skipped", kind=op.kind)
        return {
            "kind": op.kind,
            "status": "skipped",
            "reason": "handler_not_implemented",
        }

    # Per-op lifecycle events (issue #427 L4 step 2). ``tool_called`` fires
    # before the handler runs; ``tool_completed`` fires on every terminal
    # state (success / denied / skipped / failed). A per-call ``op_id``
    # ties the two events together so consumers (= TUI ToolCallRow,
    # post-hoc audit) can match start/end without ambiguity. Pure ops
    # skip emission per the registry intent (= ``OP_PURITY.pure`` =
    # no externally-observable side effect to disambiguate).
    emit_lifecycle = get_op_purity(op.kind) is not OpPurity.pure
    op_id = ""
    started_at = 0.0
    if emit_lifecycle:
        op_id = uuid.uuid4().hex[:8]
        started_at = time.monotonic()
        ctx.events.emit(
            "tool_called",
            run_id=ctx.run_id,
            skill=ctx.skill_name,
            phase=ctx.current_phase,
            kind=op.kind,
            op_id=op_id,
            args_summary=_summarize_op_args(op),
        )

    def _emit_completed(
        *, status: str, result: dict | None, error: str = "",
    ) -> None:
        if not emit_lifecycle:
            return
        ctx.events.emit(
            "tool_completed",
            run_id=ctx.run_id,
            skill=ctx.skill_name,
            phase=ctx.current_phase,
            kind=op.kind,
            op_id=op_id,
            status=status,
            duration_s=max(0.0, time.monotonic() - started_at),
            result_summary=_summarize_op_result(result) if result else "",
            error=error,
        )

    try:
        result = await handler(op, ctx, caller)
        path = getattr(op, "path", None)
        ctx.events.emit(
            "permission_granted",
            run_id=ctx.run_id,
            skill=ctx.skill_name,
            phase=ctx.current_phase,
            kind=op.kind,
            path=path,
        )
        _emit_completed(status="success", result=result)
        return result
    except PermissionError as exc:
        path = getattr(op, "path", None)
        ctx.events.emit(
            "permission_denied",
            run_id=ctx.run_id,
            skill=ctx.skill_name,
            phase=ctx.current_phase,
            kind=op.kind,
            path=path,
            reason=str(exc),
        )
        _emit_completed(status="denied", result=None, error=str(exc))
        return {"kind": op.kind, "status": "denied", "error": str(exc)}
    except OpSkipped as exc:
        ctx.events.emit("control_ir_skipped", kind=op.kind, reason=exc.reason)
        _emit_completed(status="skipped", result=None, error=exc.reason)
        return {"kind": op.kind, "status": "skipped", "reason": exc.reason}
    except Exception as exc:
        ctx.events.emit("control_ir_failed", kind=op.kind, error=str(exc))
        _emit_completed(status="failed", result=None, error=str(exc))
        return {"kind": op.kind, "status": "error", "error": str(exc)}


# Lazy-populated handler registry. Each module under op_runtime registers
# its handler at import time via `register("kind", handler)`.
_HANDLERS: dict = {}


def register(kind: str, handler) -> None:
    """Register an op handler. Called at module import time."""
    _HANDLERS[kind] = handler


def available_kinds() -> list[str]:
    """Return all registered op kinds (for diagnostics)."""
    return sorted(_HANDLERS.keys())


# Eagerly import handler modules so they self-register.
from . import ask_user as _ask_user  # noqa: F401, E402

# ADR-0033: RAG-extensible OS — embed / index_* / recall ops
from . import embed as _embed  # noqa: F401, E402
from . import file as _file  # noqa: F401, E402
from . import index_drop as _index_drop  # noqa: F401, E402
from . import index_query as _index_query  # noqa: F401, E402
from . import index_write as _index_write  # noqa: F401, E402
from . import judge_output as _judge_output  # noqa: F401, E402
from . import lint as _lint  # noqa: F401, E402
from . import mcp as _mcp  # noqa: F401, E402
from . import mcp_drop_server as _mcp_drop_server  # noqa: F401, E402
from . import mcp_install as _mcp_install  # noqa: F401, E402
from . import recall as _recall  # noqa: F401, E402
from . import run_skill as _run_skill  # noqa: F401, E402
from . import sandboxed_exec as _sandboxed_exec  # noqa: F401, E402
from . import shell as _shell  # noqa: F401, E402
from . import skill_resolve as _skill_resolve  # noqa: F401, E402
from . import web as _web  # noqa: F401, E402

__all__ = [
    "OpContext",
    "OpResult",
    "OpDenied",
    "OpSkipped",
    "OpDispatchError",
    "execute_op",
    "register",
    "available_kinds",
]
