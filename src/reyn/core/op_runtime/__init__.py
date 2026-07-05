"""Op runtime — shared backend for executing Op.

The router/chat tool path dispatches through `execute_op` here. The op kind
catalog and per-op semantics live in this package; the caller decides *when*
to invoke an op and *where* to bind its result.

`ask_user` is the only op that cannot be invoked from preprocessor
(static execution can't pause for user input). All other ops, including
side-effect ones (file.write/edit/delete, shell), are callable from both
frontends — gating is provided by the existing PermissionResolver, which
is call-site agnostic.
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import Op

from .context import OpContext
from .result import OpDenied, OpResult, OpSkipped


class OpDispatchError(RuntimeError):
    """Raised when execute_op is called with an unsupported op kind."""


async def execute_op(
    op: Op,
    ctx: OpContext,
) -> dict:
    """Dispatch a Op to its handler and return a result dict.

    Returns a JSON-serializable dict in the same shape callers have
    historically appended to `control_ir_results`. Errors are captured
    in the dict (status="error" / "denied" / "skipped"); this function
    never raises for op-level failures.

    Permission checks happen here (single point) — callers do not need to
    invoke `require_*` themselves.
    """
    handler = _HANDLERS.get(op.kind)
    if handler is None:
        ctx.events.emit("control_ir_skipped", kind=op.kind)
        return {
            "kind": op.kind,
            "status": "skipped",
            "reason": "handler_not_implemented",
        }

    try:
        result = await handler(op, ctx)
        path = getattr(op, "path", None)
        ctx.events.emit(
            "permission_granted",
            run_id=ctx.run_id,
            actor=ctx.actor,
            phase=ctx.current_phase,
            kind=op.kind,
            path=path,
        )
        return result
    except PermissionError as exc:
        path = getattr(op, "path", None)
        ctx.events.emit(
            "permission_denied",
            run_id=ctx.run_id,
            actor=ctx.actor,
            phase=ctx.current_phase,
            kind=op.kind,
            path=path,
            reason=str(exc),
        )
        return {"kind": op.kind, "status": "denied", "error": str(exc)}
    except OpSkipped as exc:
        ctx.events.emit("control_ir_skipped", kind=op.kind, reason=exc.reason)
        return {"kind": op.kind, "status": "skipped", "reason": exc.reason}
    except Exception as exc:
        ctx.events.emit("control_ir_failed", kind=op.kind, error=str(exc))
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

# #272/#1128: voluntary LLM-initiated compaction op.
from . import compact as _compact  # noqa: F401, E402

# ADR-0033: RAG-extensible OS — index_* / recall ops.
# #1303 Stage I: embed + index_write run-ops deleted (folded into
# reyn.api.safe.embed_index; recall embeds provider-direct). index_query +
# index_drop + recall remain.
from . import file as _file  # noqa: F401, E402
from . import index_drop as _index_drop  # noqa: F401, E402
from . import index_query as _index_query  # noqa: F401, E402
from . import judge_output as _judge_output  # noqa: F401, E402
from . import mcp as _mcp  # noqa: F401, E402
from . import mcp_drop_server as _mcp_drop_server  # noqa: F401, E402
from . import mcp_install as _mcp_install  # noqa: F401, E402

# #2597 slice ②a: mcp_read_resource — permission-gated resource read (mirrors mcp.py).
from . import mcp_read_resource as _mcp_read_resource  # noqa: F401, E402
from . import recall as _recall  # noqa: F401, E402
from . import sandboxed_exec as _sandboxed_exec  # noqa: F401, E402

# #2548 PR-C: local skill install op (register a SKILL.md dir into skills.entries).
from . import skill_install as _skill_install  # noqa: F401, E402

# #1953 slice 1: Task ops (first-class trackable work-units).
from . import task as _task  # noqa: F401, E402
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
