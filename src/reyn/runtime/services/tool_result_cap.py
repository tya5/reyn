"""Chat tool-result size cap — offload-based, lossless (#1128 size axis / dead-end #1).

Every chat tool-result turn is made individually compactable (≤ a bound well
under ``B_M``) so the chat retry_loop's shrink can always fold it into the
summary — closing the persistent dead-end where one huge tool result could never
be compacted away.

Mechanism: when a tool-result string exceeds the cap, the FULL body is stored via
the injected ``save_fn`` (= ``MediaStore.save_tool_result``, the #385 store —
lossless + restorable via ``MediaStore.read_tool_result``, same
``.reyn/tool-results/`` dir + path-ref shape) and the inline is replaced with a
bounded preview (head/tail + the project-relative ``_offload_ref`` path +
``_offload_content_hash``). This is now the ONE offload path (#2396): the prior
phase-axis analog (``context_builder.offload_control_ir_result``, DICT-shaped,
``.reyn/control_ir_offload/``) was retired in #2396 Step 4 once its last caller
(the ContextFrame-driven phase path) was removed by earlier convergence steps.

Offload-based, NO lossy ``[:N]`` truncation of raw content and NO store-less
discard path — the body is always recoverable, so the head/tail preview is
lossless overall (feedback_no_lossy_truncate_without_user_judgment).

Threshold is B_M-relative + token-unit (dead-end-free on ALL models, not just
large-context): ``cap_tokens = min(FIXED_CEIL_TOKENS, floor(α · effective_trigger))``
is computed by the caller (the session, which owns the engine budgets) and passed
in; this module applies it via ``estimate_tokens`` for unit consistency with the
budgets. The offloaded **preview itself** is bounded to ``≤ cap_tokens`` (not a
fixed char ceiling), so even on a small-context model the capped turn fits a
compaction call (the by-construction crux — see #1128 4585990x).
"""
from __future__ import annotations

from typing import Any, Callable

from reyn.services.compaction.engine import estimate_tokens

# Additional absolute char ceiling on the inline preview (latency / pathological
# guard), on top of the primary token-unit ≤ cap_tokens bound.
MAX_TOOL_RESULT_INLINE_BYTES: int = 16_384
_PREVIEW_HEAD_CHARS: int = 6_000
_PREVIEW_TAIL_CHARS: int = 2_000

# Cap-policy knobs (#1128 size axis).
#   ALPHA            — fraction of effective_trigger; the dead-end-safety knob.
#                      0.5 leaves headroom for the immutable previous_summary +
#                      section-caps-spec that share the compaction call's input
#                      alongside the single capped turn (≤ α·eff + summary + spec
#                      ≤ B_M).
#   FIXED_CEIL_TOKENS — upper clamp so large-context models still get a LEAN
#                      per-turn inline (cost/latency knob), not a B_M-sized one.
ALPHA: float = 0.5
FIXED_CEIL_TOKENS: int = 4096

# #5367①: the two callers of this module drive genuinely different mechanisms —
# TRIGGER_CAP is the write-time size gate (the config-default-off offload path,
# ``ContextBudgetAdvisor.cap_tool_result``); TRIGGER_OVERFLOW is the reactive
# same-turn spill a compaction/token-budget overflow provokes
# (``RouterHistoryBuffer.spill_turn_content``, #5296 PR-2). Named constants
# (not ad hoc strings at each call site) so a future third caller cannot invent
# its own spelling of either value.
TRIGGER_CAP: str = "cap"
TRIGGER_OVERFLOW: str = "overflow"


def compute_cap_tokens(
    effective_trigger: int,
    *,
    ceil_tokens: int = FIXED_CEIL_TOKENS,
    alpha: float = ALPHA,
) -> int:
    """B_M-relative per-turn cap: ``min(FIXED_CEIL_TOKENS, floor(ALPHA·effective_trigger))``.

    By construction ``< effective_trigger ≤ B_M``, so a capped tool-result turn
    always fits a single compaction call on ANY model (dead-end #1 closure,
    model-independent). Returns 0 (= cap disabled) for a non-positive trigger.
    """
    if effective_trigger <= 0:
        return 0
    return min(ceil_tokens, int(alpha * effective_trigger))


def cap_tool_result_content(
    content_str: str,
    *,
    cap_tokens: int,
    model: str,
    save_fn: Callable[..., dict],
    trigger: str,
    use_chars4: bool = False,
    events: Any = None,
    content_type: "str | None" = None,
    max_inline_bytes: int = MAX_TOOL_RESULT_INLINE_BYTES,
    preview_head_chars: int = _PREVIEW_HEAD_CHARS,
    preview_tail_chars: int = _PREVIEW_TAIL_CHARS,
    on_offload: "Callable[[str], None] | None" = None,
    on_write_unavailable: "Callable[[], None] | None" = None,
) -> str:
    """Return *content_str* unchanged if within the cap, else its offloaded plain-text preview.

    ``content_str`` is the canonical ``text`` body (#2425 案B): already the clean LLM-readable payload,
    so the full body is stored as-is (real newlines) and the inline is replaced with a bounded
    head/tail plain-text preview naming the read-back path — never a JSON stub.

    Args:
        content_str:  The tool-result text body (the canonical ``text`` stream).
        cap_tokens:   Token budget for the inline; a body estimated above it is
                      offloaded. ``<= 0`` disables the cap (identity).
        model:        Model name for ``estimate_tokens`` unit consistency.
        save_fn:      Stores the full body and returns a path-ref block with at
                      least ``"path"`` (project-relative, read back via
                      ``read_file``) and ``"content_hash"``. In production this is
                      ``MediaStore.save_tool_result`` (the #385 store) — lossless.
        trigger:      (#5367①) Which mechanism drove this call — :data:`TRIGGER_CAP`
                      (write-time size gate) or :data:`TRIGGER_OVERFLOW` (reactive
                      same-turn spill). No default: a caller that forgets it gets a
                      ``TypeError``, not a silently-unlabelled audit event.
        use_chars4:   Match the engine's token estimator (``cfg.use_chars4_estimate``)
                      so the size measurement is unit-consistent with the
                      ``effective_trigger`` budget the cap is derived from.
        events:       Optional EventLog; a ``tool_result_offloaded`` audit event
                      is emitted on offload (P6).
        content_type: (#2663) The canonical producer's declared MIME type (renderer-only sidecar,
                      e.g. ``"text/html"``), forwarded to ``save_fn``'s ``mime_type`` so the stored
                      ref's on-disk extension carries it (``None`` → the store's own
                      ``"text/plain"`` default, unchanged behaviour). Never read into ``content_str``
                      or any LLM-visible output of this function.
        on_offload:   (#5364 §1.2) Called with the offloaded ref path exactly
                      once, only when offload actually happens — the ONE
                      typed channel a caller uses to learn "this was
                      spilled, and to where" without re-deriving it by
                      parsing the returned preview text for its
                      ``read_file(path=...)`` marker (this repo's own
                      "typed, never form-sniffed" convention —
                      ``chat_message.py``'s ``TOOL_STATUS_META_KEY``
                      docstring). Optional and additive: every existing
                      caller that doesn't pass it is unaffected.
        on_write_unavailable: (#5364 §1.5) Called with no arguments when
                      ``save_fn`` raises ``MediaStoreWriteUnavailable`` —
                      the ONE typed channel a caller uses to learn "an
                      offload was attempted and refused because the
                      store's writes are known not to land," so the
                      entry can be marked accordingly instead of the
                      caller having to guess from the (unchanged, still
                      inline) return value alone. Optional and additive.

    Returns:
        The original string when ``estimate_tokens(content_str) <= cap_tokens``;
        otherwise a bounded plain-text preview (head + a truncation marker naming
        the ``read_file`` path + tail) with ``estimate_tokens(preview) <= cap_tokens``.
        The full body is always stored first — no information is lost.

        #5364 §1.5: also the original string, unchanged, if ``save_fn`` raises
        ``MediaStoreWriteUnavailable`` — "a permanently-failed write's turn
        keeps content inline, never emits a ref naming a file that doesn't
        exist" (owner). ``on_write_unavailable`` (if given) fires first.
    """
    if cap_tokens <= 0:
        return content_str
    if estimate_tokens(content_str, model, use_chars4=use_chars4) <= cap_tokens:
        return content_str

    from reyn.data.workspace.media_store import MediaStoreWriteUnavailable
    try:
        if content_type:
            block = save_fn(content_str, mime_type=content_type)
        else:
            block = save_fn(content_str)
    except MediaStoreWriteUnavailable:
        if on_write_unavailable is not None:
            on_write_unavailable()
        if events is not None:
            events.emit(
                "tool_result_write_unavailable",
                total_chars=len(content_str),
                cap_tokens=cap_tokens,
            )
        return content_str
    preview_source = content_str
    ref = block.get("path", "")
    content_hash = block.get("content_hash", "")

    def _fits(p: str) -> bool:
        # Primary bound: the offloaded preview must itself be within cap_tokens,
        # so it is < effective_trigger < B_M and therefore single-turn
        # compactable (the by-construction dead-end-#1 closure — holds on ALL
        # models, including small-context). MAX_TOOL_RESULT_INLINE_BYTES is an
        # additional absolute char ceiling (latency / pathological guard).
        return (
            estimate_tokens(p, model, use_chars4=use_chars4) <= cap_tokens
            and len(p) <= max_inline_bytes
        )

    head_chars, tail_chars = preview_head_chars, preview_tail_chars
    preview = _build_preview(
        preview_source, ref=ref,
        head_chars=head_chars, tail_chars=tail_chars,
    )
    # Shrink head/tail symmetrically until the preview fits cap_tokens. Floor at
    # 0 (= a bare lossless ref-marker) so even a tiny cap converges rather than
    # loops; the full body is always in the store regardless.
    while not _fits(preview) and (head_chars > 0 or tail_chars > 0):
        head_chars = head_chars // 2 if head_chars > 64 else 0
        tail_chars = tail_chars // 2 if tail_chars > 64 else 0
        preview = _build_preview(
            preview_source, ref=ref,
            head_chars=head_chars, tail_chars=tail_chars,
        )

    if events is not None:
        events.emit(
            "tool_result_offloaded",
            total_chars=len(preview_source),
            cap_tokens=cap_tokens,
            ref=ref,
            content_hash=content_hash,
            trigger=trigger,
        )
    if on_offload is not None:
        on_offload(ref)
    return preview


def _build_preview(
    content_str: str,
    *,
    ref: str,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Build the bounded plain-text preview for an offloaded tool result (#2425 §4).

    Readable head/tail around a single truncation marker naming the ``read_file`` read-back path —
    NOT a JSON stub. The full body is always in the store first, so the head/tail preview is lossless
    overall::

        <head>
        ...[truncated: <N> chars total — full body: read_file(path="<ref>")]...
        <tail>
    """
    head = content_str[:head_chars] if head_chars > 0 else ""
    tail = content_str[-tail_chars:] if tail_chars > 0 else ""
    marker = (
        f"...[truncated: {len(content_str)} chars total — "
        f'full body: read_file(path="{ref}")]...'
    )
    return f"{head}\n{marker}\n{tail}"
