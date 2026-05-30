"""
ContextFrame construction helpers.

Standalone functions — no runtime state. All mutable state is passed in explicitly
so this module stays testable and free of circular imports.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    Phase,
    PhaseConstraints,
)
from reyn.services.offload.store import offload_value

ARTIFACT_REF_THRESHOLD = 8000  # characters; larger artifacts are stored by ref in ContextFrame
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
    serialized = json.dumps(result, ensure_ascii=False)
    size_chars = len(serialized)
    if size_chars <= MAX_CONTROL_IR_RESULT_INLINE_BYTES:
        return result

    offload_filename = f"{result_idx:04d}_{uuid.uuid4().hex[:8]}.json"
    offload_result = offload_value(
        result,
        store_dir=offload_dir,
        preview_strategy=_phase_preview_strategy,
        filename=offload_filename,
    )

    # The preview dict produced by _phase_preview_strategy is our inline.
    inline: dict = offload_result.preview
    # Attach content_hash for verified read-back (new in Phase 1).
    inline["_offload_content_hash"] = offload_result.content_hash
    ref_path = offload_result.path_ref
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
) -> list[dict]:
    """Apply per-result offload to all control_ir_results that exceed the inline limit.

    When *offload_dir* is None, results pass through unchanged (backward compat).
    """
    if offload_dir is None:
        return control_ir_results
    return [
        offload_control_ir_result(r, i, offload_dir, events=events, phase=phase)
        for i, r in enumerate(control_ir_results)
    ]


def maybe_ref_artifact(
    artifact: dict,
    artifact_path: str | None,
    *,
    events: Any = None,
    phase: str | None = None,
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
    return {
        "type": "artifact_ref",
        "artifact_type": artifact.get("type", "unknown"),
        "ref_path": artifact_path,
        "size_bytes": len(serialized.encode("utf-8")),
    }


def build_frame(
    phase_name: str,
    phase: Phase,
    artifact: dict,
    candidates: list[CandidateOutput],
    output_language: str,
    history: list[str],
    visit_counts: dict[str, int],
    finish_criteria: list[str],
    max_phase_visits: int | None,
    available_ops: list[ControlIROpSpec],
    effective_model: str,
    model_resolved: str,
    events: Any,
    op_catalog: list[ControlIROpSpec] | None = None,
    control_ir_results: list[dict] | None = None,
    artifact_path: str | None = None,
    remaining_act_turns: int | None = None,
    run_id: str | None = None,
    act_turn: int | None = None,
    offload_dir: Path | None = None,
) -> ContextFrame:
    allowed_next = [c.next_phase for c in candidates]
    current_visit = visit_counts.get(phase_name, 1)
    total_steps = sum(visit_counts.values())

    # C5 (FP-0008): per-result offload for oversized control_ir_results.
    # A single result exceeding MAX_CONTROL_IR_RESULT_INLINE_BYTES is offloaded
    # to a workspace scratch file; the inline slot carries head+tail preview +
    # a ref path so the LLM can file.read the full content when needed.
    # Orthogonal-complementary to count-axis compaction (PR-N5 / PR-N8).
    raw_results = list(control_ir_results or [])
    offloaded_results = maybe_offload_control_ir_results(
        raw_results,
        offload_dir,
        events=events,
        phase=phase_name,
    )

    frame = ContextFrame(
        current_phase=phase_name,
        current_phase_role=phase.role,
        instructions=phase.instructions,
        input_artifact=maybe_ref_artifact(artifact, artifact_path, events=events, phase=phase_name),
        execution=ExecutionState(
            path=list(history)[-10:],
            current_visit=current_visit,
            total_steps=total_steps,
        ),
        candidate_outputs=candidates,
        finish_criteria=finish_criteria if "end" in allowed_next else [],
        constraints=PhaseConstraints(max_phase_visits=max_phase_visits),
        available_control_ops=available_ops,
        op_catalog=op_catalog or [],
        output_language=output_language,
        model=effective_model,
        model_resolved=model_resolved,
        control_ir_results=offloaded_results,
        remaining_act_turns=remaining_act_turns,
    )

    events.emit("context_built", phase=phase_name, frame=frame.model_dump(mode="json"))
    return frame
