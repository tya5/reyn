"""
ContextFrame construction helpers.

Standalone functions — no runtime state. All mutable state is passed in explicitly
so this module stays testable and free of circular imports.
"""
from __future__ import annotations

import json
from typing import Any

from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    Phase,
    PhaseConstraints,
)

ARTIFACT_REF_THRESHOLD = 8000  # characters; larger artifacts are stored by ref in ContextFrame
MAX_INLINE_BOOST = 65_536  # characters; artifacts up to 64KB are still inlined (inline-boost range)

# Per-result size cap for control_ir_results (FP-0008 PR-N v8).
# sandbox_2 v8 calibration (2026-05-28) showed file_read / shell op results
# accumulating unbounded across act turns; at N=14 turns the prompt reached
# 100K-400K tokens (396K / 366K / 217K / 113K / 101K observed), triggering
# OS-side termination. 8 KiB per result keeps a 14-turn run well under 200K
# characters of accumulated results while still fitting typical file snippets
# and shell output. The full result body is preserved in the act_executed
# event (P6 audit guarantee) and the LLM can issue a follow-up file_read with
# offset/limit if it needs more content.
MAX_CONTROL_IR_RESULT_BYTES = 8_192  # bytes; results exceeding this are truncated
_TRUNCATION_PREVIEW_BYTES = 2_048   # bytes shown at the head of a truncated result
_TRUNCATION_TAIL_BYTES = 512        # bytes shown at the tail of a truncated result


def _large_field_keys(result: dict) -> list[str]:
    """Return field names in *result* whose string values are candidates for truncation.

    We truncate string-valued fields that are likely to carry large content:
    ``content`` (file_read), ``stdout`` / ``stderr`` (shell / sandboxed_exec),
    and ``output`` (any future op that follows the same convention).  All other
    fields (status, kind, path, …) are left intact so the LLM retains
    structured metadata.
    """
    return [k for k in ("content", "stdout", "stderr", "output") if isinstance(result.get(k), str)]


def _truncate_string_to_bytes(s: str, max_bytes: int) -> tuple[str, int]:
    """Return *(truncated_str, original_byte_len)*.

    The returned string is at most *max_bytes* bytes when encoded as UTF-8.
    We do a simple encode-slice-decode so we never split a multi-byte character.
    """
    encoded = s.encode("utf-8")
    original = len(encoded)
    if original <= max_bytes:
        return s, original
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), original


def cap_control_ir_result(
    result: dict,
    idx: int,
    *,
    events: Any = None,
    phase: str | None = None,
    run_id: str | None = None,
    turn: int | None = None,
) -> dict:
    """Return *result* with large string fields truncated to MAX_CONTROL_IR_RESULT_BYTES.

    If no field exceeds the cap the original dict is returned unchanged.
    When truncation occurs:
    - Each oversized field is replaced with a structured marker string that
      includes the original byte count, a head preview, and a tail preview.
    - A ``control_ir_result_truncated`` observability event is emitted so the
      truncation is audit-visible (P6).

    The marker format is deliberately human-readable and LLM-parseable so the
    model can tell at a glance how much content was omitted and issue a
    targeted file_read with offset/limit if it needs more.
    """
    large_keys = _large_field_keys(result)
    if not large_keys:
        return result

    truncated_fields: dict[str, int] = {}
    new_result = dict(result)

    for key in large_keys:
        value: str = result[key]
        encoded = value.encode("utf-8")
        original_bytes = len(encoded)
        if original_bytes <= MAX_CONTROL_IR_RESULT_BYTES:
            continue

        # Build head + tail preview.
        head_bytes = min(_TRUNCATION_PREVIEW_BYTES, MAX_CONTROL_IR_RESULT_BYTES)
        tail_bytes = min(_TRUNCATION_TAIL_BYTES, MAX_CONTROL_IR_RESULT_BYTES - head_bytes)
        head = encoded[:head_bytes].decode("utf-8", errors="ignore")
        tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes > 0 else ""

        marker_parts = [
            f"<truncated: original {original_bytes} bytes, cap {MAX_CONTROL_IR_RESULT_BYTES} bytes>",
            f"<preview (first {len(head.encode())} bytes)>",
            head,
            "</preview>",
        ]
        if tail:
            marker_parts += [
                f"<tail (last {len(tail.encode())} bytes)>",
                tail,
                "</tail>",
            ]
        marker_parts.append(
            "<note: full content preserved in act_executed event log (P6); "
            "issue a file.read op with offset/limit for targeted access></note>"
        )
        new_result[key] = "\n".join(marker_parts)
        truncated_fields[key] = original_bytes

    if not truncated_fields:
        return result

    if events is not None:
        events.emit(
            "control_ir_result_truncated",
            phase=phase,
            run_id=run_id,
            turn=turn,
            result_idx=idx,
            result_kind=result.get("kind"),
            truncated_fields=truncated_fields,
            cap_bytes=MAX_CONTROL_IR_RESULT_BYTES,
        )

    return new_result


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
) -> ContextFrame:
    allowed_next = [c.next_phase for c in candidates]
    current_visit = visit_counts.get(phase_name, 1)
    total_steps = sum(visit_counts.values())

    # Apply per-result size cap before the results enter the LLM prompt.
    # Large file_read / shell / sandboxed_exec results can balloon the prompt
    # to 100K-400K tokens over N=14 act turns (FP-0008 PR-N v8).
    capped_results = [
        cap_control_ir_result(
            r,
            idx,
            events=events,
            phase=phase_name,
            run_id=run_id,
            turn=act_turn,
        )
        for idx, r in enumerate(control_ir_results or [])
    ]

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
        control_ir_results=capped_results,
        remaining_act_turns=remaining_act_turns,
    )

    events.emit("context_built", phase=phase_name, frame=frame.model_dump(mode="json"))
    return frame
