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
``control_ir_inline_cap`` below survives — it is still the shared read-
bounding cap consulted by ``file.py`` / ``load_skill.py``.

#4381 PR-5 (architect design, owner ruling) — the RESOURCE-BOUND / BUDGET-
BOUND role split:

    resource bound   unit = BYTES, model-INDEPENDENT, config-driven
                      protects: memory / transfer / disk
                      members: THIS cap, load_skill's cap (shares it),
                               offload's ``max_inline_bytes``
    budget bound      unit = TOKENS, model-derived
                      protects: the model's context window
                      members: offload/spill trigger, compaction,
                               reactive shrink

Before PR-5 this cap was window-derived (scaled with the resolved model's
input window, #1209) and counted in CHARACTERS. Both were architect-
identified defects: scaling a resource bound by model window conflates the
two roles above (a resource bound protects a fixed physical resource, not
a model-relative budget), and counting a byte-denominated resource in
characters drifts by up to ~3x for non-ASCII content ("多バイト文字で8192
文字≒24KBになり、資源を守る量として3倍ぶれる"). Both are fixed here: the
cap is now a plain config value (``ReadCapConfig.inline_bytes``,
model-independent) and the byte/text-length distinction is the caller's
job (``op_runtime/file.py`` / ``load_skill.py`` measure UTF-8 encoded
byte length against this cap, never ``len(str)``).

The ONE named conversion point between the two roles is
:data:`INLINE_CAP_BYTES_PER_TOKEN` below, consulted by
``router_history_buffer._check_resource_within_budget`` (PR-1, #4451) —
never re-derive an equivalent ratio anywhere else (the exact silent-drift
class #4381 exists to close).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.config.chat import ReadCapConfig

# ── control_ir_result per-result inline-cap constant (C5 — FP-0008) ────────────
# The model-independent default resource bound, in BYTES — used whenever no
# ``ReadCapConfig`` is available (a caller with no config context, or a
# ``ReadCapConfig`` was never threaded to it). #4381 PR-5: previously also
# the FLOOR for a window-derived cap that scaled with the model's input
# window (#1209) — that derivation is gone; this is now the cap, full stop,
# unless config overrides it via ``read_cap.inline_bytes``.
MAX_CONTROL_IR_RESULT_INLINE_BYTES: int = 10_240   # 10 KiB default

# #4381 PR-1/PR-5: the ONE named bytes→tokens conversion (architect design)
# — the resource boundary (this module, BYTES as of PR-5) and the budget
# boundary (`router_history_buffer.resolve_effective_trigger_and_budgets`,
# TOKENS) are compared through THIS constant and nowhere else; a second,
# independently-derived bytes-per-token ratio anywhere else would reopen
# the exact silent-drift class #4381 closes. Public (not `_`-prefixed) for
# that reason — it is a cross-module conversion point, not a local detail.
#
# Superseded name: PR-1 introduced this as ``INLINE_CAP_CHARS_PER_TOKEN``
# (chars-per-token, matching the cap's char-denominated unit at the time).
# PR-5 made the cap byte-denominated, so a *chars*-per-token ratio applied
# to a *bytes* value would silently reopen the exact unit-drift class this
# constant exists to prevent — renamed rather than reused with a new
# meaning under the old name (a literal ~4 bytes-per-token approximation
# for UTF-8-mixed text is the same rough average commonly used for chars,
# so the NUMBER is unchanged; only the unit label is corrected).
INLINE_CAP_BYTES_PER_TOKEN: int = 4


def control_ir_inline_cap(config: "ReadCapConfig | None" = None) -> int:
    """The resource-bound per-result inline cap, in BYTES.

    #4381 PR-5: model-independent — no longer derived from a resolved
    model's context window (#1209's window-derive was itself the defect
    architect's role-split closes: a resource bound protects a fixed
    physical resource, not a model-relative budget). ``config=None``
    (no ``ReadCapConfig`` threaded to the caller) falls back to
    :data:`MAX_CONTROL_IR_RESULT_INLINE_BYTES`.
    """
    if config is None:
        return MAX_CONTROL_IR_RESULT_INLINE_BYTES
    return config.inline_bytes


def byte_safe_prefix(text: str, max_bytes: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 encoding is <= ``max_bytes``,
    never splitting a multi-byte codepoint.

    #4381 PR-5: the resource bound is byte-denominated (see module
    docstring); a caller char-truncating with ``text[:max_bytes]`` was the
    exact ~3x drift architect's design closes (a multi-byte character
    counted as "1" against a byte budget) — this is the shared, tested
    replacement both ``op_runtime/file.py`` and ``load_skill.py`` use for
    their single-oversized-segment truncation branch.

    O(n): encode once, slice the encoded bytes at ``max_bytes``, then back
    off up to 3 bytes (the longest a UTF-8 codepoint can be minus one) if
    the cut landed mid-codepoint — a decode error there is expected, not
    exceptional, so this backs off rather than re-encoding a shrinking
    prefix in a loop (which would be O(n log n) for a very large single
    oversized line/segment, the exact case this helper exists for).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    for _ in range(4):  # UTF-8's longest codepoint is 4 bytes
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""
