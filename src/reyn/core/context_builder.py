"""
Control-IR result inline-cap helper.

Standalone function — no runtime state. All mutable state is passed in explicitly
so this module stays testable and free of circular imports.

#2396 Step 4: the per-result DICT-offload machinery that used to live here
(``offload_control_ir_result`` / ``maybe_offload_control_ir_results`` /
``decide_payload_field`` / ``_oversized_fields`` / ``_phase_preview_strategy`` /
``_preview_field``, plus the sibling ``maybe_ref_artifact`` artifact-ref helper)
was deleted — its last caller (the ContextFrame-driven phase path,
``phase_executor.py``) was removed by earlier convergence steps (#2397, #2412,
#1092), leaving all of it dead. The surviving offload path is the chat string
offload (``reyn.runtime.services.tool_result_cap.cap_tool_result`` →
``.reyn/tool-results/`` via ``MediaStore``), unified onto the canonical
tool-result mapper (``reyn.core.offload.canonical``) by #2648. Only
``control_ir_inline_cap`` below survives — it is still the shared window-derived
read-bounding cap consulted by ``file.py`` / ``load_skill.py``.
"""
from __future__ import annotations

from typing import Any

# ── control_ir_result per-result inline-cap constant (C5 — FP-0008) ────────────
# The floor for the window-derived per-result read-bounding cap (below). Also
# doubles as the model-unresolved default (no model context → this fixed
# 8KB floor).
MAX_CONTROL_IR_RESULT_INLINE_BYTES: int = 8_192   # ~8KB threshold

# Window-derived inline cap (#1209). The fixed 8KB above is a FLOOR; the
# effective per-result offload trigger scales with the model's input window so
# that a normal file read (e.g. a 150KB source file under a 1M-token window)
# stays INLINE instead of being offloaded out of the editing model's view. The
# fixed 8KB was a root anomaly — same class as #1201/#1172 (fixed-constant →
# window-derive). The per-RESULT cap is orthogonal to count-axis compaction,
# which still trims the TOTAL across results.
#
# #4381 PR-1: this is THE ONE named chars→tokens conversion (architect
# design) — the resource boundary (this module, chars) and the budget
# boundary (`router_history_buffer.resolve_effective_trigger_and_budgets`,
# tokens) are compared through THIS constant and nowhere else; a second,
# independently-derived chars-per-token ratio anywhere else would reopen the
# exact silent-drift class #4381 PR-1 closes. Public (not `_`-prefixed) for
# that reason — it is a cross-module conversion point, not a local detail.
INLINE_CAP_CHARS_PER_TOKEN: int = 4
_INLINE_CAP_WINDOW_FRACTION: float = 0.08  # one result may inline up to ~8% of the window


def control_ir_inline_cap(
    model_resolved: str | None,
    *,
    events: Any = None,
    phase: str | None = None,
) -> int:
    """Window-derived per-result inline cap in chars, floored at the fixed 8KB.

    ``model_resolved`` MUST be a litellm model string (already class-resolved).
    A raw model CLASS like ``"standard"`` mis-resolves to the fallback window
    (the #1201/#1172 bug) — callers pass the resolved string. ``None`` (no model
    context) falls back to the fixed floor.
    """
    if not model_resolved:
        return MAX_CONTROL_IR_RESULT_INLINE_BYTES
    from reyn.llm.model_budget import get_max_input_tokens

    t_max = get_max_input_tokens(model_resolved, events=events, phase=phase)
    derived = int(t_max * INLINE_CAP_CHARS_PER_TOKEN * _INLINE_CAP_WINDOW_FRACTION)
    return max(MAX_CONTROL_IR_RESULT_INLINE_BYTES, derived)

