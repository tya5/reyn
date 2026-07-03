"""
Control-IR result offload / inline-cap helpers.

Standalone functions — no runtime state. All mutable state is passed in explicitly
so this module stays testable and free of circular imports.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from reyn.services.offload.store import offload_value

ARTIFACT_REF_THRESHOLD = 8000  # characters; larger artifacts are stored by ref
MAX_INLINE_BOOST = 65_536  # characters; artifacts up to 64KB are still inlined (inline-boost range)

# ── control_ir_result per-result offload constants (C5 — FP-0008) ──────────────
# A single control_ir_result whose JSON serialisation exceeds
# MAX_CONTROL_IR_RESULT_INLINE_BYTES is offloaded to a workspace scratch file.
# The inline slot carries head+tail preview + a ref path so the LLM can
# file.read the full content when needed. No information is lost.
# This is orthogonal-complementary to count-axis compaction (PR-N5 / PR-N8).
MAX_CONTROL_IR_RESULT_INLINE_BYTES: int = 8_192   # ~8KB threshold
OFFLOAD_HEAD_CHARS: int = 2_048                    # first 2KB kept inline
OFFLOAD_TAIL_CHARS: int = 512                      # last 0.5KB kept inline

# Hard-bound guarantee (C5-completeness — FP-0008 v9-IN):
# After per-field type-aware previews, the offloaded inline dict MUST NOT exceed
# this limit regardless of field types (list/dict/str/nested). The whole-inline-
# replace fallback enforces this ceiling unconditionally. Value is set well above
# head+tail+small-fields overhead (~3KB) but well below any model context limit.
MAX_OFFLOADED_INLINE_BYTES: int = 16_384  # 16KB absolute ceiling for offloaded inline

# Per-field keep threshold: fields whose serialised size is below this are kept
# as-is (they're small enough not to contribute meaningfully to bloat).
_FIELD_KEEP_THRESHOLD: int = MAX_CONTROL_IR_RESULT_INLINE_BYTES

# Window-derived inline cap (#1209). The fixed 8KB above is a FLOOR; the
# effective per-result offload trigger scales with the model's input window so
# that a normal file read (e.g. a 150KB source file under a 1M-token window)
# stays INLINE instead of being offloaded out of the editing model's view. The
# fixed 8KB was a root anomaly — same class as #1201/#1172 (fixed-constant →
# window-derive). The per-RESULT cap is orthogonal to count-axis compaction,
# which still trims the TOTAL across results.
_INLINE_CAP_CHARS_PER_TOKEN: int = 4
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
    derived = int(t_max * _INLINE_CAP_CHARS_PER_TOKEN * _INLINE_CAP_WINDOW_FRACTION)
    return max(MAX_CONTROL_IR_RESULT_INLINE_BYTES, derived)


def _preview_field(value: Any, ref_path: str) -> Any:
    """Return a bounded preview of *value* for the offloaded inline dict.

    Type-aware strategy:
      - str  → head (OFFLOAD_HEAD_CHARS) + truncation marker + tail (OFFLOAD_TAIL_CHARS)
      - list → first K elements (until serialized size ~ OFFLOAD_HEAD_CHARS) +
               a sentinel string describing remaining count + ref
      - dict → first N key-value pairs (until serialized size ~ OFFLOAD_HEAD_CHARS) +
               a sentinel string describing omitted keys + ref
      - number/bool/None → returned as-is (tiny)
    """
    if isinstance(value, str):
        head = value[:OFFLOAD_HEAD_CHARS]
        has_tail = len(value) > OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS
        tail = value[-OFFLOAD_TAIL_CHARS:] if has_tail else ""
        marker = (
            f"\n... [TRUNCATED — {len(value):,} chars total; "
            f"full content at {ref_path}] ..."
        )
        return head + marker + (f"\n[TAIL PREVIEW]\n{tail}" if tail else "")

    if isinstance(value, list):
        preview_items: list = []
        accumulated = 0
        for item in value:
            item_size = len(json.dumps(item, ensure_ascii=False))
            if accumulated + item_size > OFFLOAD_HEAD_CHARS and preview_items:
                break
            preview_items.append(item)
            accumulated += item_size
        remaining = len(value) - len(preview_items)
        if remaining > 0:
            preview_items.append(
                f"...({remaining} more elements omitted; full list at {ref_path})"
            )
        return preview_items

    if isinstance(value, dict):
        preview_dict: dict = {}
        accumulated = 0
        all_keys = list(value.keys())
        for k in all_keys:
            v = value[k]
            pair_size = len(json.dumps({k: v}, ensure_ascii=False))
            if accumulated + pair_size > OFFLOAD_HEAD_CHARS and preview_dict:
                break
            preview_dict[k] = v
            accumulated += pair_size
        omitted = len(all_keys) - len(preview_dict)
        if omitted > 0:
            preview_dict["_omitted_keys"] = (
                f"({omitted} keys omitted; full dict at {ref_path})"
            )
        return preview_dict

    # number / bool / None — tiny, keep as-is
    return value


def _oversized_fields(result: dict) -> list[str]:
    """Return field names whose individual serialised size exceeds the per-field threshold.

    Covers all field types (str, list, dict, etc.) — not just strings.
    """
    return [
        k for k, v in result.items()
        if len(json.dumps(v, ensure_ascii=False)) > _FIELD_KEEP_THRESHOLD
    ]


def decide_payload_field(result: dict) -> str | None:
    """#2336: the clean-payload DECISION — the single source of truth shared by the control_ir
    offloader (below) AND the chat tool-result cap (#2394-followup, via router_loop).

    Return the op result's declared ``_offload_payload_field`` IFF it is the SOLE oversized field
    (so the offloader can store that field CLEAN — raw text with real newlines — instead of a
    whole-dict JSON envelope). Return ``None`` otherwise: no marker, or a multi-large result whose
    non-dominant large field must not be dropped to preview-only (→ whole-dict fallback, zero
    data-loss). Extracting this means the two offload paths can never diverge on the decision again
    (the divergence that let the chat path lag the control_ir fix in #2394)."""
    declared = result.get("_offload_payload_field")
    if not declared:
        return None
    return declared if _oversized_fields(result) == [declared] else None


def _phase_preview_strategy(result: dict, ref_path: str) -> dict:
    """Phase-axis preview strategy: type-aware per-field previews + hard-bound fallback.

    This is the phase-specific policy for what a bounded inline looks like.
    It is injected into the common offload infrastructure via ``offload_value``.

    Logic:
      - Small fields (≤ _FIELD_KEEP_THRESHOLD) are kept as-is.
      - Oversized fields get type-aware previews via ``_preview_field``.
      - A top-level ``_offload_ref`` is added.
      - Hard-bound guarantee: if the result still exceeds MAX_OFFLOADED_INLINE_BYTES
        (many medium-sized fields, deeply nested structures), a whole-inline fallback
        replaces the entire inline with a compact head+tail of the serialised result.
    """
    serialized = json.dumps(result, ensure_ascii=False)
    size_chars = len(serialized)

    inline: dict = {}
    large_fields = _oversized_fields(result)
    large_fields_set = set(large_fields)
    for k, v in result.items():
        if k in large_fields_set:
            inline[k] = _preview_field(v, ref_path)
        else:
            inline[k] = v

    # Top-level ref so the LLM can locate the full result with a single key.
    inline["_offload_ref"] = ref_path

    # Hard-bound guarantee: if per-field previews still exceed the ceiling
    # (many medium-sized fields, deeply nested structures, etc.), fall back to
    # a compact whole-inline representation that is guaranteed to fit.
    inline_serialized = json.dumps(inline, ensure_ascii=False)
    if len(inline_serialized) > MAX_OFFLOADED_INLINE_BYTES:
        inline = {
            "_offload_preview": serialized[:OFFLOAD_HEAD_CHARS],
            "_offload_tail": serialized[-OFFLOAD_TAIL_CHARS:],
            "_offload_total_chars": size_chars,
            "_offload_ref": ref_path,
            "_offload_note": (
                f"Result too large for field-level preview "
                f"({size_chars:,} chars); full content at {ref_path}"
            ),
        }

    return inline


def offload_control_ir_result(
    result: dict,
    result_idx: int,
    offload_dir: Path,
    *,
    events: Any = None,
    phase: str | None = None,
    cap: int = MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    on_offload_ref: Callable[[str], None] | None = None,
) -> dict:
    """Offload an oversized control_ir_result to a workspace scratch file.

    Thin wrapper around the common offload infrastructure
    (``services.offload.offload_value``) with the phase-axis preview strategy
    injected. The preview strategy applies type-aware per-field previews
    (str→head+tail, list→first K elements + count, dict→first N pairs) plus a
    hard-bound whole-inline fallback to guarantee ≤ MAX_OFFLOADED_INLINE_BYTES.

    If the JSON-serialised *result* exceeds MAX_CONTROL_IR_RESULT_INLINE_BYTES:
      - Full content is written to *offload_dir*/<idx>_<uid>.json (no info loss).
      - Each oversized field (str/list/dict) is replaced inline with a type-aware
        preview; a top-level ``_offload_ref`` key carries the absolute path.
      - ``_offload_content_hash`` is added to the inline (new: allows verified
        read-back via ``read_offloaded``).
      - Hard-bound guarantee: inline is always ≤ MAX_OFFLOADED_INLINE_BYTES.
      - A ``control_ir_result_offloaded`` event is emitted for audit visibility.

    Small results (at or below threshold) are returned unchanged (identity).
    No information is lost: the full content is retrievable via the ref path.
    """
    # #2296: a self-bounded result declares (via a positive flag) that its content is already
    # bound ≤ the inline cap BY CONSTRUCTION — its over-cap serialized size is envelope-only, so it
    # must NOT be offloaded (that would contradict #1209's keep-in-decide-context intent AND recurse:
    # its retrieval read is itself over-cap on envelope → re-offload → loop). Exempt it before the
    # size check. Generic flag (no op vocabulary) = P7-safe + open-closed: any op that self-bounds
    # sets it and is exempt without touching this code. The op's own truncation/pagination
    # (next_offset) is the correct retrieval mechanism, not an offload ref.
    if result.get("_self_bounded"):
        return result

    serialized = json.dumps(result, ensure_ascii=False)
    size_chars = len(serialized)
    if size_chars <= cap:
        return result

    offload_filename = f"{result_idx:04d}_{uuid.uuid4().hex[:8]}.json"
    # #2336: producer-declares-payload. When the op result declares a dominant
    # ``_offload_payload_field`` AND that field is the SOLE oversized field, store it
    # CLEAN (raw text with real newlines / a clean array) instead of a JSON-of-JSON
    # whole-dict envelope. Multi-large-field → whole-dict fallback (payload_field=None)
    # so a non-dominant large field's full content is never dropped to preview-only
    # (zero data-loss). P7-safe: the marker is op-supplied data, no op literal here.
    declared_payload_field = decide_payload_field(result)
    use_clean_payload = declared_payload_field is not None
    offload_result = offload_value(
        result,
        store_dir=offload_dir,
        preview_strategy=_phase_preview_strategy,
        filename=offload_filename,
        payload_field=declared_payload_field if use_clean_payload else None,
    )

    # The preview dict produced by _phase_preview_strategy is our inline.
    inline: dict = offload_result.preview
    # Attach content_hash for verified read-back (new in Phase 1).
    inline["_offload_content_hash"] = offload_result.content_hash
    if use_clean_payload:
        # Tell the reader the ref holds the raw field value (not the whole-dict envelope).
        inline["_offload_payload_field"] = declared_payload_field
        inline["_offload_ref_format"] = "raw_field"
    # #1209 (2): explicit machine-readable truncation status as a SEPARATE field
    # (the per-field previews already carry head+tail + total chars; this flags
    # the result as truncated without the model having to parse the content).
    inline["_offload_status"] = "truncated"
    inline["_offload_total_chars"] = size_chars
    ref_path = offload_result.path_ref
    # #1383 (D12): the inline carries `_offload_ref` = ref_path the LLM may
    # file.read for the full content → grant a scoped read on it.
    if on_offload_ref is not None:
        on_offload_ref(ref_path)
    large_fields = _oversized_fields(result)

    if events is not None:
        events.emit(
            "control_ir_result_offloaded",
            phase=phase,
            result_idx=result_idx,
            original_size_chars=size_chars,
            offload_path=ref_path,
            large_fields=large_fields,
        )

    return inline


def maybe_offload_control_ir_results(
    control_ir_results: list[dict],
    offload_dir: Path | None,
    *,
    events: Any = None,
    phase: str | None = None,
    cap: int = MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    on_offload_ref: Callable[[str], None] | None = None,
) -> list[dict]:
    """Apply per-result offload to all control_ir_results that exceed *cap*.

    When *offload_dir* is None, results pass through unchanged (backward compat).
    *cap* is the per-result inline trigger in chars — pass the window-derived
    value from ``control_ir_inline_cap`` so large reads stay inline on big
    windows (#1209); defaults to the fixed floor for callers without a model.
    """
    if offload_dir is None:
        return control_ir_results
    return [
        offload_control_ir_result(r, i, offload_dir, events=events, phase=phase, cap=cap,
                                  on_offload_ref=on_offload_ref)
        for i, r in enumerate(control_ir_results)
    ]


def maybe_ref_artifact(
    artifact: dict,
    artifact_path: str | None,
    *,
    events: Any = None,
    phase: str | None = None,
    on_offload_ref: Callable[[str], None] | None = None,
) -> dict:
    """
    Return the artifact as-is if small-or-medium, or an artifact_ref pointer if large.

    Decision logic:
      size <= ARTIFACT_REF_THRESHOLD            → inline (existing path)
      ARTIFACT_REF_THRESHOLD < size <= MAX_INLINE_BOOST → inline (inline-boost path)
      size > MAX_INLINE_BOOST                   → artifact_ref (existing path)

    The LLM can read artifact_ref paths via a file op. The inline-boost path
    keeps mid-size artifacts (e.g. SWE-bench task inputs, ~8–28KB) directly
    visible in the prompt so the LLM does not need a round-trip file read.

    ``on_offload_ref(ref_path)`` (#1383 D12) is invoked when an artifact_ref is
    emitted, so the OS can register a scoped read-grant: the LLM is told to read
    ``ref_path`` (often a state-dir path outside the default read zone), so it
    MUST be readable. Without this, the agent is told to read a path it is denied.
    """
    if artifact_path is None:
        return artifact
    serialized = json.dumps(artifact, ensure_ascii=False)
    size = len(serialized)
    if size <= ARTIFACT_REF_THRESHOLD:
        return artifact
    if size <= MAX_INLINE_BOOST:
        # Inline-boost: mid-size artifact stays inlined to prevent fabrication.
        # Emit an observability event so this decision is auditable.
        if events is not None:
            events.emit(
                "artifact_inline_boost",
                phase=phase,
                size_chars=size,
                artifact_path=artifact_path,
            )
        return artifact
    # artifact_ref: the LLM will be instructed to read ref_path → grant it.
    if on_offload_ref is not None:
        on_offload_ref(artifact_path)
    return {
        "type": "artifact_ref",
        "artifact_type": artifact.get("type", "unknown"),
        "ref_path": artifact_path,
        "size_bytes": len(serialized.encode("utf-8")),
    }
