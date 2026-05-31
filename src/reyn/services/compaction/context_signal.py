"""Context-size signal renderer (#272 / #1128) — the design core.

The OS injects an exact-token "how full is the context window" signal into the
router / phase prompt so the LLM can decide whether to emit a voluntary
``compact`` op before the mandatory ``retry_loop`` backstop fires. The numbers
are EXACT tokens, unit-aligned with the media load-contract error
(``free_window`` / ``media_size``), so the model reasons consistently about
"should I compact" and "what fits now".

P8-clean: OS-level vocabulary only, no skill-specific enumeration. The ``compact``
op format itself is advertised separately (tool catalog / available_control_ops);
this section only states the budget + a neutral pointer.

Shared by both axes — chat (router_loop → build_system_prompt) and phase
(runtime → phase prompt) render the identical header so the model sees a
symmetric signal everywhere.
"""
from __future__ import annotations

#: Only render the signal once the window is at least this used (= free_window
#: at or below this fraction of the trigger). Above it the window is ample, so
#: the header is omitted entirely — no per-turn SP noise / fixture churn when
#: compaction is irrelevant. The signal appears exactly when it becomes
#: actionable (the LLM still has room to act before the backstop).
_SHOW_FRACTION = 0.5
#: At or below this fraction free, the header adds an explicit compact nudge.
_LOW_FRACTION = 0.25


def render_context_size_signal(
    *, free_window: int, effective_trigger: int
) -> "str | None":
    """Render the context-size header from exact-token budget numbers.

    Args:
        free_window: exact tokens of headroom before auto-compaction fires
            (= effective_trigger − estimated current history tokens).
        effective_trigger: the total compaction threshold (window size) in
            exact tokens.

    Returns the rendered header section, or ``None`` when the window is still
    ample (free_window above ``_SHOW_FRACTION`` of the trigger) — the caller
    omits the section so the SP stays stable + noise-free until compaction
    becomes relevant.
    """
    free_window = max(0, int(free_window))
    effective_trigger = max(0, int(effective_trigger))
    if effective_trigger <= 0 or free_window > int(effective_trigger * _SHOW_FRACTION):
        return None
    used = max(0, effective_trigger - free_window)
    lines = [
        "## Context window",
        f"  - used: ~{used} tokens of ~{effective_trigger}",
        f"  - free before auto-compaction: ~{free_window} tokens",
    ]
    if free_window <= int(effective_trigger * _LOW_FRACTION):
        lines.append(
            "  - The free window is low. If you still have work to do, call "
            "`compact` to summarise older history and free room before "
            "continuing (large tool results / steps fit better afterwards)."
        )
    return "\n".join(lines)
