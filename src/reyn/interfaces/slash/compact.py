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

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


@slash(
    "compact",
    summary="Compact the conversation history now to free up context window",
    locus="session",
    usage="/compact",
    see_also=("docs/reference/runtime/control-ir.md",),
)
async def compact_cmd(ctx: "SlashContext", args: str) -> None:
    """``/compact`` — fire on-demand history compaction and report what it freed.

    Routes through the session's compaction wrapper (force_compact_now); reports
    freed tokens + the free window afterwards in exact tokens (same contract as
    the `compact` op). Fail-loud on error rather than a silent no-op.
    """
    compact_now = getattr(ctx.session, "_compact_now_for_op", None)
    if compact_now is None:
        await reply_error(
            ctx,
            "compaction is not available in this session "
            "(no compaction engine wired).",
        )
        return

    try:
        result = await compact_now()
    except Exception as exc:  # noqa: BLE001 — surface to the user, never crash the REPL
        await reply_error(ctx, f"compaction failed: {exc}")
        return

    # #191: front the chat compression metric, not router-view `freed_tokens`.
    # #5367 retired the router's own proactive elide (build_history now
    # returns the full watermark-filtered history raw), so `freed_tokens`
    # is no longer structurally pinned to ~0 for chat the way it used to be
    # (see session.py's own `_compact_now_for_op` docstring for the full
    # correction) -- but the compression metric below stays the meaningful
    # number for chat regardless: how many older turns were summarised and
    # the raw->bridge token compression.
    n = result.get("summarized_turns", 0)
    if n <= 0:
        free_after = result.get("free_window_after")
        # #5579 (owner's real machine, 2026-08-30): ``summarized_turns == 0``
        # has THREE possible causes — genuinely nothing to fold, an attempt
        # that folded nothing, or a watermark that failed to advance — and
        # this function has no way to tell them apart (``force_compact_now``
        # returns nothing; see session.py's own ``_compact_now_for_op``).
        # The PREVIOUS wording asserted "already fits the window"
        # unconditionally on ``n <= 0`` — true only for the first cause. The
        # owner's own machine showed the contradiction directly: "already
        # fits the window. Free window: ~0 tokens." in the SAME line.
        # ``free_window_after`` (``max(0, effective_trigger - after)``,
        # already computed, no new threshold needed) is the one number that
        # actually says whether the window fits: `> 0` means room remains,
        # `== 0` means it does not — regardless of why ``n`` came back 0.
        if free_after is not None and free_after <= 0:
            await reply(
                ctx,
                "Nothing was compacted this pass, and the window is still "
                "full (~0 tokens free) — /compact did not free any room. "
                "This may mean there was nothing eligible to fold, or an "
                "attempt folded nothing; either way, running /compact "
                "again right now is unlikely to help further.",
            )
            return
        tail = f" Free window: ~{free_after} tokens." if free_after is not None else ""
        await reply(
            ctx,
            "✓ Nothing to compact right now — recent history already fits the "
            "window." + tail,
        )
        return

    compressed = result.get("compressed_tokens", 0)
    bridge = result.get("bridge_tokens", 0)
    word = "turn" if n == 1 else "turns"
    await reply(
        ctx,
        f"✓ Compacted — summarised {n} older {word} (~{compressed} tokens) into a "
        f"~{bridge}-token summary bridge.",
    )
