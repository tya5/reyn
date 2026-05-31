"""Chat tool-result size cap — offload-based, lossless (#1128 size axis / dead-end #1).

Every chat tool-result turn is made individually compactable (≤ a bound well
under ``B_M``) so the chat retry_loop's shrink can always fold it into the
summary — closing the persistent dead-end where one huge tool result could never
be compacted away.

Mechanism (mirrors ``context_builder.offload_control_ir_result``, the phase
analog): when a tool-result string exceeds the cap, the FULL body is written to
``tool_results_dir`` via the shared ``services.offload.offload_value`` (lossless,
restorable through ``MediaStore.read_tool_result``) and the inline is replaced
with a bounded preview (head + tail) carrying a project-relative ``_offload_ref``
+ ``_offload_content_hash``. There is NO lossy ``[:N]`` truncation of raw content
and NO store-less discard path — the body is always recoverable, so the head/tail
preview is lossless overall (feedback_no_lossy_truncate_without_user_judgment).

Threshold is B_M-relative + token-unit (dead-end-free on ALL models, not just
large-context): ``cap_tokens = min(FIXED_CEIL_TOKENS, floor(α · effective_trigger))``
is computed by the caller (the session, which owns the engine budgets) and passed
in; this module applies it via ``estimate_tokens`` for unit consistency with the
budgets.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from reyn.services.compaction.engine import estimate_tokens
from reyn.services.offload.store import offload_value

# Hard ceiling (chars) on the inline preview, even after head/tail bounding —
# mirrors ``context_builder.MAX_OFFLOADED_INLINE_BYTES`` so a pathological
# preview can never balloon the prompt.
MAX_TOOL_RESULT_INLINE_BYTES: int = 16_384
_PREVIEW_HEAD_CHARS: int = 6_000
_PREVIEW_TAIL_CHARS: int = 2_000


def cap_tool_result_content(
    content_str: str,
    *,
    cap_tokens: int,
    model: str,
    store_dir: Path,
    use_chars4: bool = False,
    project_root: Path | None = None,
    events: Any = None,
) -> str:
    """Return *content_str* unchanged if within the cap, else its offloaded preview.

    Args:
        content_str:  The serialised tool-result text (router_loop:2148 chokepoint).
        cap_tokens:   Token budget for the inline; results estimated above it are
                      offloaded. ``<= 0`` disables the cap (identity).
        model:        Model name for ``estimate_tokens`` unit consistency.
        store_dir:    ``tool_results_dir`` — where the full body is written.
        use_chars4:   Match the engine's token estimator (``cfg.use_chars4_estimate``)
                      so the size measurement is unit-consistent with the
                      ``effective_trigger`` budget the cap is derived from.
        project_root: When given, the inline ``_offload_ref`` is made
                      project-relative so ``MediaStore.read_tool_result`` (which
                      resolves project-relative paths) can read it back.
        events:       Optional EventLog; a ``tool_result_offloaded`` audit event
                      is emitted on offload (P6).

    Returns:
        The original string when ``estimate_tokens(content_str) <= cap_tokens``;
        otherwise a bounded JSON preview string (head + tail + ``_offload_ref`` +
        ``_offload_content_hash``), guaranteed ``<= MAX_TOOL_RESULT_INLINE_BYTES``.
        The full body is always stored first — no information is lost.
    """
    if cap_tokens <= 0:
        return content_str
    if estimate_tokens(content_str, model, use_chars4=use_chars4) <= cap_tokens:
        return content_str

    result = offload_value(
        content_str,
        store_dir=store_dir,
        preview_strategy=None,  # chat axis: caller (this fn) builds the bounded preview
        filename=f"toolresult_{uuid.uuid4().hex[:8]}.txt",
    )

    ref = result.path_ref
    if project_root is not None:
        try:
            ref = str(Path(result.path_ref).resolve().relative_to(Path(project_root).resolve()))
        except ValueError:
            # Body landed outside project_root (unusual) — keep the absolute ref.
            ref = result.path_ref

    def _fits(p: str) -> bool:
        # Primary bound: the offloaded preview must itself be within cap_tokens,
        # so it is < effective_trigger < B_M and therefore single-turn
        # compactable (the by-construction dead-end-#1 closure — holds on ALL
        # models, including small-context, not just large). MAX_TOOL_RESULT_INLINE_BYTES
        # is an additional absolute char ceiling (latency / pathological guard).
        return (
            estimate_tokens(p, model, use_chars4=use_chars4) <= cap_tokens
            and len(p) <= MAX_TOOL_RESULT_INLINE_BYTES
        )

    head_chars, tail_chars = _PREVIEW_HEAD_CHARS, _PREVIEW_TAIL_CHARS
    preview = _build_preview(
        content_str, ref=ref, content_hash=result.content_hash,
        head_chars=head_chars, tail_chars=tail_chars,
    )
    # Shrink head/tail symmetrically until the preview fits cap_tokens. Floor at
    # 0 (= a bare marker with no head/tail) so even a tiny cap converges to the
    # lossless ref-only marker rather than looping; the full body is always in
    # the store regardless.
    while not _fits(preview) and (head_chars > 0 or tail_chars > 0):
        head_chars = head_chars // 2 if head_chars > 64 else 0
        tail_chars = tail_chars // 2 if tail_chars > 64 else 0
        preview = _build_preview(
            content_str, ref=ref, content_hash=result.content_hash,
            head_chars=head_chars, tail_chars=tail_chars,
        )

    if events is not None:
        events.emit(
            "tool_result_offloaded",
            total_chars=len(content_str),
            cap_tokens=cap_tokens,
            ref=ref,
            content_hash=result.content_hash,
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
