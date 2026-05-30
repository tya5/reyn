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


def _oversized_string_fields(result: dict) -> list[str]:
    """Return field names whose string value alone exceeds the inline limit."""
    return [
        k for k, v in result.items()
        if isinstance(v, str) and len(v) > MAX_CONTROL_IR_RESULT_INLINE_BYTES
    ]


def offload_control_ir_result(
    result: dict,
    result_idx: int,
    offload_dir: Path,
    *,
    events: Any = None,
    phase: str | None = None,
) -> dict:
    """Offload an oversized control_ir_result to a workspace scratch file.

    If the JSON-serialised *result* exceeds MAX_CONTROL_IR_RESULT_INLINE_BYTES:
      - Full content is written to *offload_dir*/<idx>_<uid>.json.
      - Each string field larger than the threshold is replaced inline with
        a head+tail preview and a truncation marker referencing the offload file.
      - A top-level ``_offload_ref`` key carries the absolute path for direct
        file.read access.
      - A ``control_ir_result_offloaded`` event is emitted for audit visibility.

    Small results (at or below threshold) are returned unchanged (identity).
    No information is lost: the full content is retrievable via the ref path.
    """
    serialized = json.dumps(result, ensure_ascii=False)
    size_chars = len(serialized)
    if size_chars <= MAX_CONTROL_IR_RESULT_INLINE_BYTES:
        return result

    # Write full content to workspace scratch — no information loss.
    offload_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:8]
    offload_filename = f"{result_idx:04d}_{uid}.json"
    offload_path = offload_dir / offload_filename
    offload_path.write_text(serialized, encoding="utf-8")
    ref_path = str(offload_path)

    # Build inline preview: copy result, replace oversized string fields
    # with head + tail preview + truncation marker + ref path.
    inline = dict(result)
    large_fields = _oversized_string_fields(result)
    for field in large_fields:
        original = result[field]
        head = original[:OFFLOAD_HEAD_CHARS]
        has_tail = len(original) > OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS
        tail = original[-OFFLOAD_TAIL_CHARS:] if has_tail else ""
        marker = (
            f"\n... [TRUNCATED — {len(original):,} chars total; "
            f"full content at {ref_path}] ..."
        )
        inline[field] = head + marker + (f"\n[TAIL PREVIEW]\n{tail}" if tail else "")

    # Top-level ref so the LLM can locate the full result with a single key.
    inline["_offload_ref"] = ref_path

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
