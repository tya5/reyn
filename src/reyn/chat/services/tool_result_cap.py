"""Chat tool-result size cap — offload-based, lossless (#1128 size axis / dead-end #1).

Every chat tool-result turn is made individually compactable (≤ a bound well
under ``B_M``) so the chat retry_loop's shrink can always fold it into the
summary — closing the persistent dead-end where one huge tool result could never
be compacted away.

Mechanism (mirrors ``context_builder.offload_control_ir_result``, the phase
analog): when a tool-result string exceeds the cap, the FULL body is stored via
the injected ``save_fn`` (= ``MediaStore.save_tool_result``, the #385 store —
lossless + restorable via ``MediaStore.read_tool_result``, same
``.reyn/tool-results/`` dir + path-ref shape) and the inline is replaced with a
bounded preview (head/tail + the project-relative ``_offload_ref`` path +
``_offload_content_hash``).

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

import json
from typing import Any, Callable

from reyn.services.compaction.engine import estimate_tokens

# Additional absolute char ceiling on the inline preview (latency / pathological
# guard), on top of the primary token-unit ≤ cap_tokens bound. Mirrors
# ``context_builder.MAX_OFFLOADED_INLINE_BYTES``.
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


def compute_cap_tokens(effective_trigger: int) -> int:
    """B_M-relative per-turn cap: ``min(FIXED_CEIL_TOKENS, floor(ALPHA·effective_trigger))``.

    By construction ``< effective_trigger ≤ B_M``, so a capped tool-result turn
    always fits a single compaction call on ANY model (dead-end #1 closure,
    model-independent). Returns 0 (= cap disabled) for a non-positive trigger.
    """
    if effective_trigger <= 0:
        return 0
    return min(FIXED_CEIL_TOKENS, int(ALPHA * effective_trigger))


def cap_tool_result_content(
    content_str: str,
    *,
    cap_tokens: int,
    model: str,
    save_fn: Callable[[str], dict],
    use_chars4: bool = False,
    events: Any = None,
) -> str:
    """Return *content_str* unchanged if within the cap, else its offloaded preview.

    Args:
        content_str:  The serialised tool-result text (router_loop:2148 chokepoint).
        cap_tokens:   Token budget for the inline; results estimated above it are
                      offloaded. ``<= 0`` disables the cap (identity).
        model:        Model name for ``estimate_tokens`` unit consistency.
        save_fn:      Stores the full body and returns a path-ref block with at
                      least ``"path"`` (project-relative, read back via
                      ``MediaStore.read_tool_result``) and ``"content_hash"``.
                      In production this is ``MediaStore.save_tool_result`` (the
                      #385 store) — lossless, never truncating.
        use_chars4:   Match the engine's token estimator (``cfg.use_chars4_estimate``)
                      so the size measurement is unit-consistent with the
                      ``effective_trigger`` budget the cap is derived from.
        events:       Optional EventLog; a ``tool_result_offloaded`` audit event
                      is emitted on offload (P6).

    Returns:
        The original string when ``estimate_tokens(content_str) <= cap_tokens``;
        otherwise a bounded JSON preview string (head + tail + ``_offload_ref`` +
        ``_offload_content_hash``) with ``estimate_tokens(preview) <= cap_tokens``.
        The full body is always stored first — no information is lost.
    """
    if cap_tokens <= 0:
        return content_str
    if estimate_tokens(content_str, model, use_chars4=use_chars4) <= cap_tokens:
        return content_str

    block = save_fn(content_str)
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
            and len(p) <= MAX_TOOL_RESULT_INLINE_BYTES
        )

    head_chars, tail_chars = _PREVIEW_HEAD_CHARS, _PREVIEW_TAIL_CHARS
    preview = _build_preview(
        content_str, ref=ref, content_hash=content_hash,
        head_chars=head_chars, tail_chars=tail_chars,
    )
    # Shrink head/tail symmetrically until the preview fits cap_tokens. Floor at
    # 0 (= a bare lossless ref-marker) so even a tiny cap converges rather than
    # loops; the full body is always in the store regardless.
    while not _fits(preview) and (head_chars > 0 or tail_chars > 0):
        head_chars = head_chars // 2 if head_chars > 64 else 0
        tail_chars = tail_chars // 2 if tail_chars > 64 else 0
        preview = _build_preview(
            content_str, ref=ref, content_hash=content_hash,
            head_chars=head_chars, tail_chars=tail_chars,
        )

    if events is not None:
        events.emit(
            "tool_result_offloaded",
            total_chars=len(content_str),
            cap_tokens=cap_tokens,
            ref=ref,
            content_hash=content_hash,
        )
    return preview


def _build_preview(
    content_str: str,
    *,
    ref: str,
    content_hash: str,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Build the bounded inline preview JSON for an offloaded tool result."""
    return json.dumps(
        {
            "_offload_ref": ref,
            "_offload_content_hash": content_hash,
            "_offload_total_chars": len(content_str),
            "_offload_note": (
                f"Tool result offloaded ({len(content_str)} chars); read the full "
                f"body via read_tool_result({ref!r})."
            ),
            "_offload_head": content_str[:head_chars] if head_chars > 0 else "",
            "_offload_tail": content_str[-tail_chars:] if tail_chars > 0 else "",
        },
        ensure_ascii=False,
    )
