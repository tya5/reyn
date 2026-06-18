"""``/compact`` — compact the conversation history now to free context window.

The fourth user-facing avoidance mechanism for the conversation-window
dead-end (#191): the LLM-judgment route (the `compact` op) and the mandatory
`retry_loop` backstop already exist; this gives the **user** on-demand control,
matching the window-utilization-first compaction policy (#1185) where the user
decides when to spend a compaction rather than aggressive auto-compaction
imposing it.

Unlike the `compact` op (LLM-emitted, routed through the op runtime), this is
user input → it calls the session-level compaction directly. It reuses
``Session._compact_now_for_op`` (the same `force_compact_now` wrapper the
compact op uses), so the freed-token report is the **same contract** as the op:
``{freed_tokens, free_window_after}``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


@slash(
    "compact",
    summary="Compact the conversation history now to free up context window",
    usage="/compact",
    see_also=("docs/reference/runtime/control-ir.md",),
)
async def compact_cmd(session: "Session", args: str) -> None:
    """``/compact`` — fire on-demand history compaction and report what it freed.

    Routes through the session's compaction wrapper (force_compact_now); reports
    freed tokens + the free window afterwards in exact tokens (same contract as
    the `compact` op). Fail-loud on error rather than a silent no-op.
    """
    compact_now = getattr(session, "_compact_now_for_op", None)
    if compact_now is None:
        await reply_error(
            session,
            "compaction is not available in this session "
            "(no compaction engine wired).",
        )
        return

    try:
        result = await compact_now()
    except Exception as exc:  # noqa: BLE001 — surface to the user, never crash the REPL
        await reply_error(session, f"compaction failed: {exc}")
        return

    # #191: front the chat compression metric, not router-view `freed_tokens`
    # (structurally ~0 for chat — the router prompt is head+tail TURN-bounded, so
    # compaction COMPRESSES the already-elided middle into a summary bridge rather
    # than shrinking the bounded view). What's meaningful: how many older turns
    # were summarised and the raw→bridge token compression.
    n = result.get("summarized_turns", 0)
    if n <= 0:
        free_after = result.get("free_window_after")
        tail = f" Free window: ~{free_after} tokens." if free_after is not None else ""
        await reply(
            session,
            "✓ Nothing to compact right now — recent history already fits the "
            "window." + tail,
        )
        return

    compressed = result.get("compressed_tokens", 0)
    bridge = result.get("bridge_tokens", 0)
    word = "turn" if n == 1 else "turns"
    await reply(
        session,
        f"✓ Compacted — summarised {n} older {word} (~{compressed} tokens) into a "
        f"~{bridge}-token summary bridge.",
    )
