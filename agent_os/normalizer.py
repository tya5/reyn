"""
OS Output Normalization Layer

Converts raw, untrusted LLM output into a strict execution contract.
App-agnostic, Phase-agnostic, DSL-agnostic.

Input:  raw dict from LLM, list of allowed next_phase values (including "end")
Output: NormalizationResult with a ControlDecision and artifact

Preferred format (strict — validated fully):
  {
    "control": {
      "type": "transition|finish|abort",
      "decision": "continue|revise|finish|abort",
      "next_phase": "<phase_name>|null",
      "confidence": 0.0-1.0,
      "reason": {"summary": "..."}
    },
    "artifact": {"type": "...", "data": {...}},
    "control_ir": []
  }

Legacy backward-compat format (synthesized — no strict validation):
  {"next_phase": "...", "artifact": {...}, "confidence": ..., "reason": "..."}
  {"status": "finish", "final_output": {...}}
  {"status": "transition", "next_phase": "..."}
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from .models import ControlDecision, ControlReason


class NormalizationError(Exception):
    pass


class ControlIRValidationError(Exception):
    """Raised when a control block fails strict field or consistency validation."""
    pass


@dataclass
class NormalizationResult:
    control: ControlDecision
    artifact: dict[str, Any] = field(default_factory=dict)
    control_ir: list[Any] = field(default_factory=list)
    # provenance
    was_normalized: bool = False    # control was recovered from non-canonical field
    original_raw_type: str | None = None  # what LLM sent if different (e.g. "end", "finish")
    was_inferred: bool = False      # control was inferred from single candidate


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _find_decision(raw: dict) -> str | None:
    """Look for a decision value in artifact.data or top-level data."""
    for container in (raw.get("artifact"), raw):
        if not isinstance(container, dict):
            continue
        data = container.get("data", {})
        if isinstance(data, dict) and "decision" in data:
            return data["decision"]
    return None


def _extract_artifact(raw: dict) -> dict:
    """
    Extract the artifact from raw output.
    Falls back to reconstructing from top-level fields when the LLM
    flattened the artifact directly into the response object.
    """
    if "artifact" in raw:
        return raw["artifact"]
    excluded = {"next_phase", "status", "control_ir", "reason", "confidence",
                "final_output", "artifact", "control"}
    return {k: v for k, v in raw.items() if k not in excluded}


def _extract_legacy_meta(raw: dict, artifact: dict) -> tuple[float, ControlReason]:
    """Extract confidence and reason from legacy top-level fields for synthesis."""
    raw_conf = raw.get("confidence")
    confidence: float = float(raw_conf) if raw_conf is not None else 1.0

    reason_val = raw.get("reason")
    if not reason_val:
        data = artifact.get("data", {}) if isinstance(artifact, dict) else {}
        reason_val = data.get("reason")

    if confidence == 1.0 and raw_conf is None:
        data = artifact.get("data", {}) if isinstance(artifact, dict) else {}
        art_conf = data.get("confidence")
        if art_conf is not None:
            confidence = float(art_conf)

    summary = reason_val if isinstance(reason_val, str) else ""
    return confidence, ControlReason(summary=summary)


def _infer_legacy_decision(
    ctrl_type: str,
    next_phase: str | None,
    allowed_next_phases: list[str],
) -> str:
    """Synthesize decision from legacy output fields (format translation, not inference)."""
    if ctrl_type == "abort":
        return "abort"
    if ctrl_type == "finish":
        return "finish"
    # transition
    if next_phase == "revise":
        return "revise"
    return "continue"


# ── Strict new-format validation ───────────────────────────────────────────────

_REQUIRED_CONTROL_FIELDS = ("type", "decision", "next_phase", "confidence", "reason")
_VALID_TYPES = ("transition", "finish", "abort")
_VALID_DECISIONS = ("continue", "revise", "finish", "abort")


def _validate_control_ir_strict(control: dict) -> None:
    """
    Validate a control block against the strict spec.
    Raises ControlIRValidationError on any violation.
    """
    # 1. Required fields
    for field_name in _REQUIRED_CONTROL_FIELDS:
        if field_name not in control:
            raise ControlIRValidationError(
                f"control.{field_name} is required but missing"
            )

    # 2. Type checks
    ctrl_type = control["type"]
    if ctrl_type not in _VALID_TYPES:
        raise ControlIRValidationError(
            f"control.type {ctrl_type!r} is not valid. Expected: {_VALID_TYPES}"
        )

    ctrl_decision = control["decision"]
    if ctrl_decision not in _VALID_DECISIONS:
        raise ControlIRValidationError(
            f"control.decision {ctrl_decision!r} is not valid. Expected: {_VALID_DECISIONS}"
        )

    confidence = control["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ControlIRValidationError(
            f"control.confidence must be a number, got {type(confidence).__name__}"
        )
    if not (0.0 <= float(confidence) <= 1.0):
        raise ControlIRValidationError(
            f"control.confidence {confidence} is out of range [0.0, 1.0]"
        )

    reason = control["reason"]
    if not isinstance(reason, dict):
        raise ControlIRValidationError(
            f"control.reason must be an object with a 'summary' field, "
            f"got {type(reason).__name__}"
        )
    if "summary" not in reason:
        raise ControlIRValidationError(
            "control.reason.summary is required but missing"
        )
    if not isinstance(reason["summary"], str):
        raise ControlIRValidationError(
            f"control.reason.summary must be a string, got {type(reason['summary']).__name__}"
        )

    # 3. Consistency checks
    if ctrl_type == "finish":
        if ctrl_decision != "finish":
            raise ControlIRValidationError(
                f"control.type='finish' requires control.decision='finish', "
                f"got {ctrl_decision!r}"
            )
        if control["next_phase"] is not None:
            raise ControlIRValidationError(
                "control.type='finish' requires control.next_phase=null"
            )

    if ctrl_type == "transition":
        if not control["next_phase"]:
            raise ControlIRValidationError(
                "control.type='transition' requires a non-empty control.next_phase"
            )

    if ctrl_decision == "revise":
        if ctrl_type != "transition":
            raise ControlIRValidationError(
                f"control.decision='revise' requires control.type='transition', "
                f"got {ctrl_type!r}"
            )
        if control["next_phase"] != "revise":
            raise ControlIRValidationError(
                f"control.decision='revise' requires control.next_phase='revise', "
                f"got {control['next_phase']!r}"
            )

    if ctrl_type == "abort":
        if ctrl_decision != "abort":
            raise ControlIRValidationError(
                f"control.type='abort' requires control.decision='abort', "
                f"got {ctrl_decision!r}"
            )


# ── New-format normalizer ──────────────────────────────────────────────────────

def _normalize_new_format(raw: dict, allowed_next_phases: list[str]) -> NormalizationResult:
    """
    Normalize when LLM used the new {control, artifact} format.
    Applies strict validation — raises ControlIRValidationError on any violation.
    """
    control_raw = raw.get("control", {})
    if not isinstance(control_raw, dict):
        raise ControlIRValidationError(
            f"'control' must be a JSON object, got {type(control_raw).__name__}"
        )

    _validate_control_ir_strict(control_raw)

    ctrl_type = control_raw["type"]
    ctrl_decision = control_raw["decision"]
    ctrl_next = control_raw["next_phase"]
    confidence = float(control_raw["confidence"])
    ctrl_reason = ControlReason(summary=control_raw["reason"]["summary"])
    artifact = _extract_artifact(raw)

    if ctrl_type == "abort":
        control = ControlDecision(
            type="abort", decision="abort", next_phase=None,
            confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(control=control, artifact=artifact, control_ir=raw.get("control_ir", []))

    if ctrl_type == "finish":
        if "end" not in allowed_next_phases:
            if len(allowed_next_phases) == 1:
                # LLM tried to finish but it's not allowed — only option is to transition
                forced_next = allowed_next_phases[0]
                forced_decision = _infer_legacy_decision("transition", forced_next, allowed_next_phases)
                control = ControlDecision(
                    type="transition", decision=forced_decision,
                    next_phase=forced_next, confidence=confidence, reason=ctrl_reason,
                )
                return NormalizationResult(
                    control=control, artifact=artifact,
                    control_ir=raw.get("control_ir", []),
                    was_normalized=True, original_raw_type="finish",
                )
            raise NormalizationError(
                "LLM chose control.type='finish' but the current phase is not allowed to finish. "
                f"Allowed: {allowed_next_phases}."
            )
        control = ControlDecision(
            type="finish", decision="finish", next_phase=None,
            confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(control=control, artifact=artifact, control_ir=raw.get("control_ir", []))

    # transition
    if ctrl_next in allowed_next_phases:
        control = ControlDecision(
            type="transition", decision=ctrl_decision,
            next_phase=ctrl_next, confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(control=control, artifact=artifact, control_ir=raw.get("control_ir", []))

    # next_phase not in allowed
    if len(allowed_next_phases) == 1 and allowed_next_phases[0] != "end":
        forced_next = allowed_next_phases[0]
        forced_decision = _infer_legacy_decision("transition", forced_next, allowed_next_phases)
        control = ControlDecision(
            type="transition", decision=forced_decision,
            next_phase=forced_next, confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(
            control=control, artifact=artifact,
            control_ir=raw.get("control_ir", []),
            was_normalized=True, original_raw_type=f"transition/{ctrl_next}",
        )

    raise NormalizationError(
        f"control.next_phase {ctrl_next!r} is not a valid candidate. "
        f"Allowed: {allowed_next_phases}."
    )


# ── Legacy-format normalizer ───────────────────────────────────────────────────

def _normalize_legacy(raw: dict, allowed_next_phases: list[str]) -> NormalizationResult:
    """
    Synthesize a ControlDecision from the old next_phase / status format.
    Produces a full ControlDecision (including decision + ControlReason) via format translation.
    """
    next_phase = raw.get("next_phase")

    def _make(
        phase: str,
        artifact: dict,
        was_normalized: bool = False,
        original: str | None = None,
        was_inferred: bool = False,
    ) -> NormalizationResult:
        confidence, ctrl_reason = _extract_legacy_meta(raw, artifact)
        ctrl_type = "finish" if phase == "end" else "transition"
        ctrl_next = None if phase == "end" else phase
        ctrl_decision = _infer_legacy_decision(ctrl_type, ctrl_next, allowed_next_phases)
        control = ControlDecision(
            type=ctrl_type, decision=ctrl_decision,
            next_phase=ctrl_next, confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(
            control=control, artifact=artifact,
            control_ir=raw.get("control_ir", []),
            was_normalized=was_normalized, original_raw_type=original, was_inferred=was_inferred,
        )

    # Rule 1: canonical
    if next_phase is not None and next_phase in allowed_next_phases:
        return _make(str(next_phase), _extract_artifact(raw))

    # Rule 2: next_phase == "end" but end not allowed
    if next_phase == "end":
        if len(allowed_next_phases) == 1:
            return _make(allowed_next_phases[0], _extract_artifact(raw), was_normalized=True, original="end")
        raise NormalizationError(
            "LLM returned next_phase='end' but the current phase is not allowed to finish. "
            f"Allowed: {allowed_next_phases}."
        )

    # Rule 3: old status field
    status = raw.get("status")
    if status is not None and status in allowed_next_phases:
        artifact = _extract_artifact(raw)
        if status == "finish" and "end" in allowed_next_phases:
            final_out = raw.get("final_output")
            if final_out is not None:
                artifact = final_out
        return _make(str(status), artifact, was_normalized=True, original=str(next_phase) if next_phase is not None else None)

    # Rule 3b: status == "finish" + "end" allowed
    if status == "finish" and "end" in allowed_next_phases:
        final_out = raw.get("final_output") or _extract_artifact(raw)
        artifact = final_out if isinstance(final_out, dict) else {}
        confidence, ctrl_reason = _extract_legacy_meta(raw, artifact)
        control = ControlDecision(
            type="finish", decision="finish", next_phase=None,
            confidence=confidence, reason=ctrl_reason,
        )
        return NormalizationResult(
            control=control, artifact=artifact,
            control_ir=raw.get("control_ir", []),
            was_normalized=True, original_raw_type="finish",
        )

    # Rule 3c: status == "transition"
    if status == "transition" and next_phase in allowed_next_phases:
        return _make(str(next_phase), _extract_artifact(raw), was_normalized=True, original=None)

    # Rule 4: data.decision
    decision = _find_decision(raw)
    if decision in allowed_next_phases:
        return _make(decision, _extract_artifact(raw), was_normalized=True, original=str(next_phase) if next_phase is not None else None)

    # Rule 5 & 6: next_phase absent
    if next_phase is None:
        if len(allowed_next_phases) == 1:
            return _make(allowed_next_phases[0], _extract_artifact(raw), was_inferred=True)
        if len(allowed_next_phases) == 0:
            raise NormalizationError(
                "LLM returned no next_phase and current phase has no candidate outputs."
            )
        raise NormalizationError(
            f"LLM returned no next_phase and {len(allowed_next_phases)} candidates are possible "
            f"{allowed_next_phases} — cannot infer."
        )

    # Rule 7: next_phase present but not in allowed — single-candidate force
    if len(allowed_next_phases) == 1:
        return _make(allowed_next_phases[0], _extract_artifact(raw), was_normalized=True, original=str(next_phase))

    raise NormalizationError(
        f"next_phase {next_phase!r} is not a valid candidate. "
        f"Allowed: {allowed_next_phases}."
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def normalize(
    raw: dict,
    allowed_next_phases: list[str],
) -> NormalizationResult:
    """
    Normalize raw LLM output into a NormalizationResult.
    Raises ControlIRValidationError when a control block fails strict validation.
    Raises NormalizationError when intent cannot be determined.

    allowed_next_phases: list of valid next_phase values (phase names + "end" if applicable)
    """
    if "control" in raw:
        return _normalize_new_format(raw, allowed_next_phases)
    return _normalize_legacy(raw, allowed_next_phases)
