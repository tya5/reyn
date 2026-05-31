"""compact op — voluntary, LLM-initiated history compaction (#272 / #1128).

When the OS-injected context-size signal shows the window is filling, the model
may emit a ``compact`` control_ir op instead of waiting for the mandatory
``retry_loop`` backstop. The op routes through ``ctx.compact_now`` — an
awaitable the caller (ChatSession / phase runtime) wires to its existing
synchronous compaction (``force_compact_now``). The result reports freed tokens
and the free window afterwards in EXACT tokens, unit-aligned with the
context-size signal + the media load-contract error, so the model reasons
consistently about "should I compact" and "what fits now".

P7/P8: no skill-specific strings. The op is OS-level vocabulary only; the
optional ``reason`` is model-supplied audit text the OS never interprets.

OpPurity: external (LLM cost + history mutation; the inner compaction engine
emits its own events).
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import CompactIROp

from . import register
from .context import OpContext


async def handle(
    op: CompactIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Run a voluntary compaction via ``ctx.compact_now``.

    Returns a result dict; never raises. If no compaction capability is wired
    (``ctx.compact_now is None``) the op returns a clear error rather than a
    silent no-op (same contract as ask_user without an intervention_bus) so the
    model isn't misled into thinking the window was freed.
    """
    if ctx.compact_now is None:
        ctx.events.emit("compact_op_unavailable", run_id=ctx.run_id, phase=ctx.current_phase)
        return {
            "kind": "compact",
            "status": "error",
            "error_kind": "compaction_unavailable",
            "error": (
                "compact requested but no compaction context is wired here "
                "(ctx.compact_now is None). Voluntary compaction is only "
                "available where a session/phase compaction engine exists."
            ),
        }

    ctx.events.emit(
        "compact_op_requested",
        run_id=ctx.run_id,
        phase=ctx.current_phase,
        reason=op.reason,
    )
    try:
        result = await ctx.compact_now()
    except Exception as exc:  # noqa: BLE001 — surface as op error, never crash the turn
        ctx.events.emit(
            "compact_op_failed", run_id=ctx.run_id, phase=ctx.current_phase, error=str(exc)
        )
        return {
            "kind": "compact",
            "status": "error",
            "error_kind": "compaction_failed",
            "error": str(exc),
        }

    # ``result`` carries exact-token fields from the caller's wrapper
    # ({freed_tokens, free_window_after, ...}). Pass them through verbatim so
    # the unit-alignment with the context-size signal is preserved.
    out: dict = {"kind": "compact", "status": "ok"}
    out.update(result or {})
    ctx.events.emit(
        "compact_op_completed",
        run_id=ctx.run_id,
        phase=ctx.current_phase,
        freed_tokens=out.get("freed_tokens"),
        free_window_after=out.get("free_window_after"),
    )
    return out


register("compact", handle)
