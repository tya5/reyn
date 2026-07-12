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
# its handler at import time via `register("kind", handler, canonical=…)`.
_HANDLERS: dict = {}


def register(kind: str, handler, *, canonical) -> None:
    """Register an op handler + its canonical declaration (FP-0056 PR-F1).

    ``canonical`` is REQUIRED: a mapper (``result -> CanonicalToolResult``), the ``STRUCTURED_PASSTHROUGH``
    opt-in, or the provisional ``CANONICAL_TODO`` marker. The declaration is born WITH the op registration (not a
    free-floating ``_MAPPERS`` dict hand-synced elsewhere), so an op kind can never reach the offload
    chokepoint without a declared LLM-visible shape — the structural gap the 2026-07-09 dogfood
    incident exposed. The coverage gate (``tests/test_fp0056_canonical_coverage_gate.py``) enumerates
    every registered kind and asserts the declaration exists."""
    from reyn.core.offload.canonical import declare_canonical

    _HANDLERS[kind] = handler
    declare_canonical(kind, canonical)


def available_kinds() -> list[str]:
    """Return all registered op kinds (for diagnostics)."""
    return sorted(_HANDLERS.keys())


# Eagerly import handler modules so they self-register.
from . import ask_user as _ask_user  # noqa: F401, E402

# #272/#1128: voluntary LLM-initiated compaction op.
from . import compact as _compact  # noqa: F401, E402

# ADR-0033: RAG-extensible OS — index_* / semantic_search ops.
# #1303 Stage I: embed + index_write run-ops deleted (folded into
# reyn.api.safe.embed_index; semantic_search embeds provider-direct,
# per-source-model). index_query + index_drop + semantic_search remain.
# FP-0057 Phase 1: embed op RE-ADDED as the user-facing raw embedding
# primitive (#1303's "no caller" rationale is obsolete post skill-engine
# deletion, #2438).
# FP-0057 Phase 2a: index_update — incremental/delta-reconcile ingestion,
# the shared `embed` op's SECOND internal caller (alongside
# semantic_search's query embed). `recall` renamed `semantic_search`
# (clean-break, fixes the recall/search_actions/memory naming collision).
# FP-0057 Phase 2b: `reyn.api.safe.embed_index.embed_and_index` (the
# CodeAct-only entry #1303 folded here) retired clean-break — replaced by
# `reyn.api.safe.index_update`, a thin safe-mode dispatch onto this SAME
# `index_update` op (no more provider-direct embed path from safe-mode
# python steps).
from . import embed as _embed  # noqa: F401, E402

# Hook-Event Redesign Phase 5 part 2 (proposal 0059 §8): emit_hook_event —
# LLM-authored hook-event emission onto the caller's own HookBus.
from . import emit_hook_event as _emit_hook_event  # noqa: F401, E402
from . import file as _file  # noqa: F401, E402
from . import index_drop as _index_drop  # noqa: F401, E402
from . import index_query as _index_query  # noqa: F401, E402
from . import index_update as _index_update  # noqa: F401, E402
from . import judge_output as _judge_output  # noqa: F401, E402
from . import mcp as _mcp  # noqa: F401, E402
from . import mcp_drop_server as _mcp_drop_server  # noqa: F401, E402

# #2597 slice ②c: mcp_get_prompt — permission-gated prompt fetch (mirrors mcp_read_resource.py).
from . import mcp_get_prompt as _mcp_get_prompt  # noqa: F401, E402
from . import mcp_install as _mcp_install  # noqa: F401, E402

# #2597 slice ②a: mcp_read_resource — permission-gated resource read (mirrors mcp.py).
from . import mcp_read_resource as _mcp_read_resource  # noqa: F401, E402

# #2597 slice ②b: resource subscriptions — subscribe/unsubscribe (mirrors mcp_read_resource.py).
from . import mcp_subscribe_resource as _mcp_subscribe_resource  # noqa: F401, E402
from . import mcp_unsubscribe_resource as _mcp_unsubscribe_resource  # noqa: F401, E402

# pipeline install op (register a pipeline DSL file into pipelines.entries — mirrors skill_install).
from . import pipeline_install as _pipeline_install  # noqa: F401, E402

# FP-0054 PR-A: present op — user-facing presentation of bulk data (null renderer).
from . import present as _present  # noqa: F401, E402

# proposal 0060 Phase 1 Layer A (A8): present-view install op (register a named
# presentation template into presentations.entries — mirrors skill_install/
# pipeline_install; lower threat, validate_blueprint is the structural gate).
from . import presentation_install as _presentation_install  # noqa: F401, E402

# FP-0055 PR-2: render_template op — sandboxed Jinja2 text-templating producer.
from . import render_template as _render_template  # noqa: F401, E402
from . import sandboxed_exec as _sandboxed_exec  # noqa: F401, E402
from . import semantic_search as _semantic_search  # noqa: F401, E402

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
