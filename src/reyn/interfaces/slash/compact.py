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

#5888 (owner real-machine incident: "ctx 75% なのに ... ユーザは圧縮したい
のにできない") — two defects, one report:

1. **The reply described a quantity it had never measured.** Its only
   number was ``free_window_after`` = ``effective_trigger − history
   estimate``: the room left in the MIDDLE once head/tail/system-prompt
   budgets are subtracted. It rendered that as "the window is still full
   (~0 tokens free)" — a claim about the model's context window, which
   the status bar was simultaneously (and correctly) showing as 75%
   used. Every reply now ends with :func:`_measured_line`: window
   used/size, middle room/used, what head/tail protected, and the seq the
   summary already covers — each named as itself.
2. **"There was nothing eligible to fold" was false.** It fired whenever
   no candidate was SELECTED, which included the owner's actual case:
   plenty eligible, all of it held back by head/tail protection. Those
   are now two different sentences, told apart by ``eligible_count``
   (see the branches at the end of :func:`compact_cmd`), and this
   command now asks for ``selection="operator"`` so an explicit
   ``/compact`` folds the whole unprotected middle instead of only the
   reactive ladder's shortfall (#5719's rule, which exists to stop the
   ladder's own over-fold and was never a reason to refuse an operator).

3. **(stage 2, #5888 3″) Zero fold candidates was not the floor either.**
   Rung ① SPILL now runs on that SAME pass (``CompactionController.
   force_compact_now``'s own ``if not candidates:`` branch) — it swaps a
   tool result's body for a reference without splitting the message, so
   it reaches content head/tail is protecting WITHOUT loosening that
   protection (#2289's keep-whole invariant is untouched; spill runs
   orthogonal to it). A 3 MB tool result sitting in the tail — the
   owner's own real-machine case — is exactly what this reaches. When
   spilling made progress, the reply says so instead of "all protected";
   when the driver has no spill capability at all (#5717), it says that
   too, rather than folding both into one silent "nothing to compact".
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


def _measured_line(result: dict) -> str:
    """#5888 ruling 1: the four quantities this compaction pass measured,
    each named as itself — window used/size, middle room/used, what
    head/tail protected, and how far the existing summary already
    reaches.

    Rendered from ``Session._compact_now_for_op``'s own returned numbers
    (which come from the SAME selection pass that decided what to fold,
    and — for the window pair — from the SAME accessors the status-bar
    ctx chip reads). Nothing here is re-derived locally: a number this
    line prints is a number the code acted on.

    Degrades a field at a time rather than all-or-nothing: a caller whose
    result predates these keys (or a session with no LLM call yet, so no
    ``prompt_tokens``) simply gets the parts that ARE known. An empty
    string when nothing is known at all — never a fabricated zero, which
    would read as a real measurement of "nothing".
    """
    parts: list[str] = []
    window = result.get("window_tokens") or 0
    used = result.get("window_used_tokens") or 0
    if window:
        pct = f" ({used * 100 // window}%)" if used else ""
        parts.append(f"window {used}/{window} tokens{pct}")
    if "middle_room_tokens" in result or "middle_used_tokens" in result:
        room = result.get("middle_room_tokens") or 0
        middle_used = result.get("middle_used_tokens") or 0
        parts.append(f"middle room {room}, used {middle_used}")
    if "protected_head_tokens" in result or "protected_tail_tokens" in result:
        head_t = result.get("protected_head_tokens") or 0
        tail_t = result.get("protected_tail_tokens") or 0
        turns = (result.get("protected_head_turns") or 0) + (
            result.get("protected_tail_turns") or 0
        )
        parts.append(
            f"protected: head {head_t} + tail {tail_t} tokens ({turns} turns)"
        )
    if "covers_through_seq" in result:
        parts.append(f"already folded through seq {result.get('covers_through_seq') or 0}")
    return (" — " + " · ".join(parts)) if parts else ""


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
        # #5888 ruling 2: an operator's explicit /compact is a request to
        # SHRINK, not a fit-check. `selection="operator"` folds the whole
        # unprotected, uncovered middle; the reactive ladder keeps the
        # `"shortfall"` default (#5719 non-regression). See
        # `CompactionController._measure_and_select`.
        result = await compact_now(selection="operator")
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
    # #5888 ruling 1: every reply below ends with the four quantities this
    # pass actually measured, each named as ITSELF. The pre-#5888 reply had
    # exactly one number (`free_window_after`) and used it to make a claim
    # about a different quantity — "the window is still full (~0 tokens
    # free)" while the status bar read ctx 75%, because `free_window_after`
    # is `effective_trigger - history estimate` (the room left in the
    # MIDDLE after head/tail/system-prompt budgets), not the model's
    # context window at all. Owner's own words: "ctx 75% なのに下記メッセ
    # ージ出るのも謎。ユーザは圧縮したいのにできない".
    measured = _measured_line(result)
    free_tail = f" Free window: ~{free_after} tokens." if free_after is not None else ""

    if n > 0:
        compressed = result.get("compressed_tokens", 0)
        bridge = result.get("bridge_tokens", 0)
        word = "turn" if n == 1 else "turns"
        await reply(
            ctx,
            f"✓ Compacted — summarised {n} older {word} (~{compressed} tokens) into a "
            f"~{bridge}-token summary bridge." + measured,
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
            "recovery_summary_persisted for detail." + measured + free_tail,
        )
        return

    # #5888 ruling 1: these last two branches used to be told apart by
    # `free_window_after`, which answers neither question. They are now
    # told apart by `eligible_count` — the number that actually decides
    # which of the two happened:
    #
    #   eligible_count == 0  → nothing was ELIGIBLE. The only state where
    #                          "nothing eligible to fold" is true.
    #   eligible_count > 0   → things were eligible and head/tail PROTECTED
    #     with candidate_count == 0  all of them. A different fact, and the one the owner
    #                          actually hit; saying "nothing eligible" here
    #                          told them their history was empty of
    #                          foldable content when it was full of it.
    #
    # `free_window_after` still travels (in `free_tail`) as the orthogonal
    # fact #5579 established it as — but it no longer DECIDES anything,
    # and it never again supplies the words "the window is full": that
    # sentence described the model's context window using a number
    # measured against the compaction trigger, which is exactly how a
    # 75%-full window got reported as full (#5888).
    eligible = result.get("eligible_count")
    # #5888 3″ (stage 2): on the SAME zero-candidate pass, rung① spill
    # ran too (`CompactionController.force_compact_now`'s own `if not
    # candidates:` branch) — read its report BEFORE deciding which
    # "nothing was folded" sentence applies, so a pass that genuinely
    # freed something never gets described as having done nothing.
    spilled_count = result.get("spilled_count") or 0
    if eligible is not None and eligible > 0 and spilled_count > 0:
        chars_freed = result.get("spilled_chars_freed") or 0
        result_word = "result" if spilled_count == 1 else "results"
        await reply(
            ctx,
            f"✓ Nothing was folded, but spilled {spilled_count} tool "
            f"{result_word} (~{chars_freed} chars) out of the protected "
            "head/tail groups — their bodies are now references, freeing "
            "that much from the wire without loosening protection." +
            measured + free_tail,
        )
        return
    if eligible is not None and eligible > 0:
        # Eligible entries existed; head/tail protection held every one of
        # them back, and rung① spill (#5888 3″, above) either found
        # nothing eligible to spill or has no capability on this driver
        # at all (#5717) — named explicitly so an operator with no spill
        # mechanism is told THAT, not left to guess why nothing changed.
        word = "entry" if eligible == 1 else "entries"
        spill_note = (
            " (spill is not available on this path)"
            if not result.get("spill_capability_present", True) else ""
        )
        await reply(
            ctx,
            f"Nothing was folded: all {eligible} eligible {word} are "
            "protected by the head/tail keep-window, so there was no "
            f"unprotected middle left to compact{spill_note}." + measured + free_tail,
        )
        return
    await reply(
        ctx,
        "✓ Nothing to compact right now — no history entry is eligible to "
        "fold yet (everything is either already folded into the summary or "
        "is not compactable content)." + measured + free_tail,
    )
