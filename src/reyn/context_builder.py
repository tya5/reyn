"""
ContextFrame construction helpers.

Standalone functions — no runtime state. All mutable state is passed in explicitly
so this module stays testable and free of circular imports.
"""
from __future__ import annotations

import json
from typing import Any

import litellm

from reyn.llm.model_budget import get_max_input_tokens
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

# Compaction safety margin: trigger compaction when estimated prompt size
# exceeds 85% of the model's max_input_tokens. 0.85 is intentionally
# conservative — we want a meaningful buffer below the hard limit so that the
# system prompt + LLM response tokens do not push the total over the cap.
# YAGNI: not exposed as a config knob; 0.85 is universally appropriate for the
# "drop oldest results" strategy (= no risk of over-compacting because each
# retained result still leaves headroom for the LLM's own output).
_COMPACTION_SAFETY_MARGIN = 0.85

# Placeholder shape injected for a dropped result.  Contains only fields that
# are safe to expose without the content body.  Preserves LLM visibility of
# "an op happened at this position" without re-including the large payload.
_COMPACTED_RESULT_PLACEHOLDER_KEYS = ("kind", "status")


def _estimate_tokens(text: str, model: str) -> int:
    """Estimate token count for *text* against *model*.

    Uses ``litellm.token_counter`` for accurate per-model token counts when
    the model is known to LiteLLM.  Falls back to ``len(text) / 4`` (= rough
    chars-per-token estimate) when ``token_counter`` returns 0 or raises.

    The fallback is intentionally conservative (4 chars/token is below the
    average for English prose at ~4.5 chars/token) so estimates err toward
    compacting more rather than less.
    """
    try:
        count = litellm.token_counter(model=model, text=text)
        if count and count > 0:
            return count
    except Exception:
        pass
    # Fallback: 4 chars ≈ 1 token (conservative estimate)
    return max(1, len(text) // 4)


def _make_placeholder(result: dict, idx: int, original_bytes: int) -> dict:
    """Return a small placeholder dict preserving audit fields, dropping content."""
    placeholder: dict = {"compacted": True, "result_idx": idx, "original_bytes": original_bytes}
    for key in _COMPACTED_RESULT_PLACEHOLDER_KEYS:
        if key in result:
            placeholder[key] = result[key]
    return placeholder


def compact_control_ir_results(
    results: list[dict],
    model: str,
    frame_json_without_results: str,
    *,
    events: Any = None,
    phase: str | None = None,
    run_id: str | None = None,
    turn: int | None = None,
) -> list[dict]:
    """Selectively drop oldest results until estimated prompt size fits the model budget.

    Algorithm:
      1. Compute the model's token budget threshold (max_input_tokens × safety_margin).
      2. Serialize the current results list and combine with the rest of the
         frame JSON to get the estimated total prompt size.
      3. If estimated tokens ≤ threshold: return results unchanged.
      4. Otherwise: replace the OLDEST result with a compact placeholder and
         re-estimate. Repeat until within budget or all results are compacted.
      5. Emit a ``control_ir_results_compacted`` observability event (P6) when
         any compaction occurs.

    The full result body is preserved in the ``act_executed`` event (P6 audit
    guarantee) — this function only modifies the in-prompt copy.

    Parameters
    ----------
    results:
        The list of control_ir_results to potentially compact.
    model:
        LiteLLM model string used for token counting and budget query.
    frame_json_without_results:
        JSON serialization of the ContextFrame *excluding* the results field,
        used to estimate the non-results portion of the prompt.
    events:
        Optional EventLog for emitting the compaction event.
    phase / run_id / turn:
        Observability fields passed through to the emitted event.
    """
    if not results:
        return results

    max_input_tokens = get_max_input_tokens(model, events=events, phase=phase, run_id=run_id)
    threshold = int(max_input_tokens * _COMPACTION_SAFETY_MARGIN)

    # Estimate current total prompt size (base frame + results).
    results_json = json.dumps(results, ensure_ascii=False)
    combined_text = frame_json_without_results + results_json
    estimated_tokens_before = _estimate_tokens(combined_text, model)

    if estimated_tokens_before <= threshold:
        return results

    # Compaction needed: work on a mutable copy.
    working = list(results)
    compacted_count = 0

    for idx in range(len(working)):
        if estimated_tokens_before <= threshold:
            break
        original = working[idx]
        if original.get("compacted"):
            continue  # already a placeholder — skip
        original_bytes = len(json.dumps(original, ensure_ascii=False).encode("utf-8"))
        working[idx] = _make_placeholder(original, idx, original_bytes)
        compacted_count += 1

        # Re-estimate after each replacement.
        results_json = json.dumps(working, ensure_ascii=False)
        combined_text = frame_json_without_results + results_json
        estimated_tokens_before = _estimate_tokens(combined_text, model)

    estimated_tokens_after = estimated_tokens_before

    if compacted_count > 0 and events is not None:
        events.emit(
            "control_ir_results_compacted",
            phase=phase,
            run_id=run_id,
            turn=turn,
            compacted_count=compacted_count,
            estimated_tokens_before=_estimate_tokens(
                frame_json_without_results + json.dumps(results, ensure_ascii=False), model
            ),
            estimated_tokens_after=estimated_tokens_after,
            max_input_tokens=max_input_tokens,
            safety_margin=_COMPACTION_SAFETY_MARGIN,
        )

    return working


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

    # Build the base frame without control_ir_results first so we can estimate
    # the non-results portion of the prompt for compaction threshold calculation.
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
        control_ir_results=[],
        remaining_act_turns=remaining_act_turns,
    )

    # Apply model-aware compaction before results enter the LLM prompt.
    # Compaction replaces the oldest results with compact placeholders when
    # the estimated prompt token count would exceed the model's input budget
    # (= max_input_tokens × safety_margin).  The full result body is preserved
    # in the act_executed event (P6 audit guarantee).
    raw_results = list(control_ir_results or [])
    if raw_results:
        frame_json_base = json.dumps(frame.model_dump(mode="json"), ensure_ascii=False)
        compacted_results = compact_control_ir_results(
            raw_results,
            model=effective_model,
            frame_json_without_results=frame_json_base,
            events=events,
            phase=phase_name,
            run_id=run_id,
            turn=act_turn,
        )
    else:
        compacted_results = []

    # Rebuild frame with (possibly compacted) results.
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
        control_ir_results=compacted_results,
        remaining_act_turns=remaining_act_turns,
    )

    events.emit("context_built", phase=phase_name, frame=frame.model_dump(mode="json"))
    return frame
