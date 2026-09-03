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
    free_after = result.get("free_window_after")
    free_tail = f" Free window: ~{free_after} tokens." if free_after is not None else ""

    if n > 0:
        compressed = result.get("compressed_tokens", 0)
        bridge = result.get("bridge_tokens", 0)
        word = "turn" if n == 1 else "turns"
        await reply(
            ctx,
            f"✓ Compacted — summarised {n} older {word} (~{compressed} tokens) into a "
            f"~{bridge}-token summary bridge.",
        )
        return

    # #5708 (owner real-machine incident, #5579's own follow-up): `n == 0`
    # used to collapse THREE distinct causes into one hedged sentence — the
    # owner's own machine showed the contradiction directly ("already fits
    # the window. Free window: ~0 tokens." in the same line). `force_compact_
    # now` (via `Session._compact_now_for_op`) now RETURNS which of its 4
    # outcomes actually fired, so each gets its own, non-hedged wording —
    # never "may mean X or Y". `free_window`/`free_after` is an ORTHOGONAL
    # fact (#5708 acceptance ③): appended where it adds information, never
    # used to infer WHY nothing was summarised.
    outcome = result.get("compaction_outcome")
    candidate_count = result.get("compaction_candidate_count", 0)

    if outcome == "already_running":
        await reply(
            ctx,
            "Another compaction pass is already running — try /compact "
            "again once it finishes." + free_tail,
        )
        return

    if outcome == "compaction_input_gap_invariant_violated":
        # A defensive invariant, not a routine outcome (compaction_
        # controller.py's own comment on this branch) — an operator seeing
        # this has hit something unexpected, not "nothing to do".
        await reply_error(
            ctx,
            "compaction could not run: an internal consistency check "
            "failed (compaction_input_gap_invariant_violated). This is "
            "unexpected — please report it.",
        )
        return

    if outcome == "forced_sync" and candidate_count > 0:
        # #5708 acceptance ②: distinguishes THIS case (candidates were
        # selected, an attempt ran) from `forced_sync_no_turns`/
        # `candidate_count == 0` below (nothing was ever selected) — the
        # exact distinction `summarized_turns == 0` alone could not make.
        count_word = f"{candidate_count} candidate{'s' if candidate_count != 1 else ''}"
        if result.get("compaction_failed"):
            # #5708 acceptance ④: `_run_compaction` raised (swallowed,
            # #5633 — the exception itself never reaches this caller,
            # only the fact that it happened does). State it plainly —
            # no "may indicate", the caller asked for a fact, not a
            # guess.
            await reply_error(
                ctx,
                f"Compaction failed while processing {count_word} — no "
                "summary was persisted. Check the audit log for "
                "compaction_failed for details.",
            )
            return
        # No exception, but the watermark still did not advance — a
        # genuinely unresolved case (this IS the honest limit of what
        # `force_compact_now` currently reports back); the hedge stays
        # here, narrowed to only this one residual unknown rather than
        # spread across every `n <= 0` result the way it used to be.
        await reply(
            ctx,
            f"An attempt to compact ran ({count_word}), but it did not "
            "advance — no summary was persisted, though no failure was "
            "recorded either. Check the audit log for compaction_check/"
            "recovery_summary_persisted for detail." + free_tail,
        )
        return

    # outcome in {"forced_sync_no_turns", "forced_sync" with candidate_
    # count == 0} (or an old-shaped result with no `compaction_outcome` at
    # all — a legacy caller) — genuinely nothing eligible to fold (no
    # candidates were ever selected), the one case the pre-#5708 wording
    # was actually correct for.
    #
    # #5579 (acceptance ③, kept unchanged): `free_window_after` is an
    # ORTHOGONAL fact from WHY nothing was selected — a genuinely-empty
    # candidate set can still coincide with a window that has NOT
    # recovered any room (`free_after <= 0`, the owner's own observed
    # contradiction: "already fits the window. Free window: ~0 tokens."
    # in the same line). Keep the two wordings distinct here, exactly as
    # #5579 fixed them — this branch only decides "nothing was eligible",
    # never "the window has room".
    if free_after is not None and free_after <= 0:
        await reply(
            ctx,
            "Nothing was compacted this pass, and the window is still "
            "full (~0 tokens free) — /compact did not free any room. "
            "There was nothing eligible to fold.",
        )
        return
    await reply(
        ctx,
        "✓ Nothing to compact right now — recent history already fits the "
        "window." + free_tail,
    )
