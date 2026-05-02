"""
OS Output Normalization Layer

Converts raw, untrusted LLM output into a strict execution contract.
App-agnostic, Phase-agnostic, DSL-agnostic.

The LLM is told to emit one of two shapes (see llm.py system prompt):

  Act turn:    {"type": "act", "ops": [...]}
  Decide turn: {"type": "decide", "control": {...}, "artifact": {...}, "ops": []}

Act turns are handled by the runtime before reaching here. The normalizer
is for decide turns. The `control` block is required; if it's missing or
malformed the runtime's `_run_decide_with_retry` will re-prompt the LLM.

The normalizer keeps two small "rescue" paths for cases where the LLM
emits a structurally valid control block but picks a next_phase that
isn't in the allowed list — when there's exactly one valid alternative,
we forgive it. Anything else falls through to the retry loop.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

import pydantic

from reyn.schemas.models import ControlDecision, ControlReason


class NormalizationError(Exception):
    pass


class ControlIRValidationError(Exception):
    """Raised when a control block fails strict field or consistency validation."""
    pass


@dataclass
class NormalizationResult:
    control: ControlDecision
    artifact: dict[str, Any] = field(default_factory=dict)
    ops: list[Any] = field(default_factory=list)
    # provenance — set when the normalizer rescued an LLM mistake
    was_normalized: bool = False
    original_raw_type: str | None = None
    was_inferred: bool = False  # reserved; no longer set, kept for event-schema stability


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_artifact(raw: dict) -> dict:
    """Extract the artifact from raw output.

    Falls back to reconstructing from top-level fields when the LLM
    flattened the artifact directly into the response object.
    """
    if "artifact" in raw:
        return raw["artifact"]
    excluded = {"type", "next_phase", "status", "ops", "reason", "confidence",
                "final_output", "artifact", "control"}
    return {k: v for k, v in raw.items() if k not in excluded}


# ── Validation ────────────────────────────────────────────────────────────────


def _parse_control(control_raw: dict) -> ControlDecision:
    """Parse a control dict into a ControlDecision.

    Field shape (required keys, types, Literal membership) is enforced by
    Pydantic. We re-raise as ControlIRValidationError so callers don't have
    to know about Pydantic.
    """
    try:
        return ControlDecision.model_validate(control_raw)
    except pydantic.ValidationError as exc:
        # Surface the first error path concisely; full detail available via __cause__.
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        msg = first.get("msg", "invalid")
        raise ControlIRValidationError(f"control.{loc}: {msg}") from exc


def _check_consistency(control: ControlDecision) -> None:
    """Enforce cross-field rules that don't fall out of Pydantic types alone.

    - confidence in [0.0, 1.0]
    - (type, decision, next_phase) must be a coherent triple
    """
    if not (0.0 <= control.confidence <= 1.0):
        raise ControlIRValidationError(
            f"control.confidence {control.confidence} is out of range [0.0, 1.0]"
        )

    t, d, n = control.type, control.decision, control.next_phase

    if t == "rollback":
        if n is not None:
            raise ControlIRValidationError(
                "control.type='rollback' requires control.next_phase=null — "
                "the OS determines the rollback target from execution history"
            )
        return  # rollback has its own decision rules; no further checks

    if t == "finish":
        if d != "finish":
            raise ControlIRValidationError(
                f"control.type='finish' requires control.decision='finish', got {d!r}"
            )
        if n is not None:
            raise ControlIRValidationError(
                "control.type='finish' requires control.next_phase=null"
            )
        return

    if t == "abort":
        if d != "abort":
            raise ControlIRValidationError(
                f"control.type='abort' requires control.decision='abort', got {d!r}"
            )
        return

    if t == "transition":
        if not n:
            raise ControlIRValidationError(
                "control.type='transition' requires a non-empty control.next_phase"
            )


# ── Normalization ─────────────────────────────────────────────────────────────


def normalize(raw: dict, allowed_next_phases: list[str]) -> NormalizationResult:
    """Normalize raw LLM decide-turn output into a NormalizationResult.

    Raises ControlIRValidationError when the control block is missing or
    fails strict validation. Raises NormalizationError when the LLM's
    chosen next_phase / type can't be mapped to any allowed candidate.

    `allowed_next_phases` is the list of valid `next_phase` values the
    runtime offered (phase names + "end" if finishing is allowed).
    """
    control_raw = raw.get("control")
    if control_raw is None:
        raise ControlIRValidationError(
            "decide-turn output missing required 'control' block"
        )
    if not isinstance(control_raw, dict):
        raise ControlIRValidationError(
            f"'control' must be a JSON object, got {type(control_raw).__name__}"
        )

    control = _parse_control(control_raw)
    _check_consistency(control)

    artifact = _extract_artifact(raw)
    ops = raw.get("ops", [])

    if control.type == "abort":
        return NormalizationResult(control=control, artifact=artifact, ops=ops)

    if control.type == "rollback":
        # Rollback ignores the artifact — the OS routes back to the previous phase.
        return NormalizationResult(control=control, artifact={}, ops=ops)

    if control.type == "finish":
        if "end" in allowed_next_phases:
            return NormalizationResult(control=control, artifact=artifact, ops=ops)
        # Rescue: LLM tried to finish but it's not allowed. If there's exactly
        # one transition candidate, force it.
        if len(allowed_next_phases) == 1:
            forced = allowed_next_phases[0]
            patched = control.model_copy(update={
                "type": "transition", "decision": "continue", "next_phase": forced,
            })
            return NormalizationResult(
                control=patched, artifact=artifact, ops=ops,
                was_normalized=True, original_raw_type="finish",
            )
        raise NormalizationError(
            "LLM chose control.type='finish' but the current phase is not allowed to finish. "
            f"Allowed: {allowed_next_phases}."
        )

    # transition
    if control.next_phase in allowed_next_phases:
        return NormalizationResult(control=control, artifact=artifact, ops=ops)

    # Rescue: LLM picked a non-allowed next_phase, but only one transition is valid.
    if len(allowed_next_phases) == 1 and allowed_next_phases[0] != "end":
        forced = allowed_next_phases[0]
        original = control.next_phase
        patched = control.model_copy(update={"next_phase": forced})
        return NormalizationResult(
            control=patched, artifact=artifact, ops=ops,
            was_normalized=True, original_raw_type=f"transition/{original}",
        )

    raise NormalizationError(
        f"control.next_phase {control.next_phase!r} is not a valid candidate. "
        f"Allowed: {allowed_next_phases}."
    )
