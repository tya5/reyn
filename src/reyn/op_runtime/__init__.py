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

from typing import Literal

from reyn.schemas.models import ControlIROp

from .context import OpContext
from .result import OpDenied, OpResult, OpSkipped


class OpDispatchError(RuntimeError):
    """Raised when execute_op is called with an unsupported op kind."""


_PREPROCESSOR_FORBIDDEN_KINDS = frozenset({"ask_user"})


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

    try:
        return await handler(op, ctx, caller)
    except PermissionError as exc:
        path = getattr(op, "path", None)
        ctx.events.emit("permission_denied", kind=op.kind, path=path, reason=str(exc))
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
from . import file as _file  # noqa: F401, E402
from . import lint as _lint  # noqa: F401, E402
from . import mcp as _mcp  # noqa: F401, E402
from . import mcp_install as _mcp_install  # noqa: F401, E402
from . import run_skill as _run_skill  # noqa: F401, E402
from . import shell as _shell  # noqa: F401, E402
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
