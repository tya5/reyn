"""PhaseExecutor — Layer 2 of OSRuntime decomposition.

Extracted from OSRuntime (FP-0020 Component C). Owns driving one phase
to completion via act/decide loops with retry.

Responsibilities:
- Phase-budget wall-clock enforcement (_check_phase_budget) — moved UP from
  the Component B shim in runtime.py._call_llm_and_record so that
  LLMCallRecorder has no dependency on phase_started_at. The check now
  happens before each LLM call at this layer.
- Act-turn loop until the LLM emits a decide-turn (_run_act_loop).
- Decide-turn validation with retry (_run_decide_with_retry).
- Single-attempt output validation (_validate_phase_output).
- Entry point: execute() composes the three methods above.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pydantic

from reyn.kernel.normalizer import (
    ControlIRValidationError,
    NormalizationError,
    NormalizationResult,
    normalize,
)
from reyn.kernel.runtime_types import (
    PhaseBudgetExceededError,
    WorkflowAbortedError,
    _normalize_artifact,
    _validate_artifact_structure,
)
from reyn.kernel.validation import ValidationError, validate_output
from reyn.safety.limit_handler import LimitDecision, handle_limit_exceeded
from reyn.schemas.models import ActOutput, CandidateOutput, LLMOutput
from reyn.workspace.artifact_validator import validate_artifact_data

if TYPE_CHECKING:
    from reyn.kernel.llm_call_recorder import LLMCallRecorder
    from reyn.kernel.run_state import RunState
    from reyn.schemas.models import Skill
    from reyn.user_intervention import InterventionBus

_log = logging.getLogger(__name__)


class PhaseExecutor:
    """Drives one phase to completion via act/decide loops with retry.

    Extracted from OSRuntime._execute_phase, _run_act_loop,
    _run_decide_with_retry, _validate_phase_output (FP-0020 Component C).

    Constructor dependencies:
      llm_caller         — LLMCallRecorder (Component B); called for each LLM turn.
      control_ir_executor — ControlIRExecutor; executes act-turn ops.
      events             — EventLog; all state-change events emitted here.
      skill              — Skill definition (phases, permissions, graph).
      safety             — SafetyConfig; phase_seconds budget + on_limit policy.
      intervention_bus   — InterventionBus | None; for safety-limit checkpoints.

    Public surface: execute() → (NormalizationResult, LLMOutput, retry_count).
    """

    def __init__(
        self,
        *,
        llm_caller: "LLMCallRecorder",
        control_ir_executor,
        events,
        skill: "Skill",
        safety,
        intervention_bus: "InterventionBus | None",
        run_id: str | None = None,
        strict: bool = False,
        build_frame_fn,
    ) -> None:
        self._llm_caller = llm_caller
        self._control_ir_executor = control_ir_executor
        self._events = events
        self._skill = skill
        self._safety = safety
        self._intervention_bus = intervention_bus
        self._run_id = run_id
        self._strict = strict
        # build_frame is owned by OSRuntime (accesses skill graph + resolver +
        # event history). PhaseExecutor receives it as a callable to avoid
        # pulling the full OSRuntime dependency graph into this module.
        self._build_frame = build_frame_fn

    # ── Phase-budget enforcement (moved up from Component B shim) ─────────────

    async def _check_phase_budget(
        self,
        phase_name: str,
        state: "RunState",
    ) -> None:
        """Wall-clock budget check before each LLM call.

        Raises PhaseBudgetExceededError when over budget, unless the
        safety.on_limit policy approves continuation.

        Behavioral note (FP-0020-C): this method was previously called inside
        OSRuntime._call_llm_and_record (Component B shim). Moving it here means
        the check runs at the PhaseExecutor layer — BEFORE passing to
        LLMCallRecorder.call() — so LLMCallRecorder has no dependency on
        phase_started_at. Observable timing is identical: the check fires
        immediately before each LLM call.
        """
        budget = self._safety.timeout.phase_seconds
        if not budget or state.phase_started_at is None:
            return
        elapsed = state.elapsed_phase_seconds()
        effective_budget = state.effective_phase_budget(budget)
        if elapsed <= effective_budget:
            return

        decision: LimitDecision = await handle_limit_exceeded(
            bus=self._intervention_bus,
            on_limit=self._safety.on_limit,
            kind="phase_seconds",
            run_id=self._run_id or "",
            prompt=(
                f"Phase {phase_name!r} ran for {elapsed:.1f}s, exceeding "
                f"the {effective_budget:.1f}s budget. Allow longer?"
            ),
            detail=f"phase={phase_name} elapsed={elapsed:.2f} budget={effective_budget:.2f}",
            extension_amount=float(budget),
        )
        if decision.allow_continue:
            state.grant_extension("phase_seconds", decision.extension)
            state.reset_phase_clock()
            self._events.emit(
                "safety_limit_checkpoint",
                kind="phase_seconds",
                allow_continue=True,
                reason=decision.reason,
                extension=decision.extension,
            )
            return

        self._events.emit(
            "safety_limit_checkpoint",
            kind="phase_seconds",
            allow_continue=False,
            reason=decision.reason,
            extension=decision.extension,
        )
        self._events.emit(
            "phase_budget_exceeded",
            phase=phase_name, elapsed=elapsed, budget=effective_budget,
        )
        raise PhaseBudgetExceededError(phase_name, elapsed, effective_budget)

    # ── Single-attempt output validation ──────────────────────────────────────

    def _validate_phase_output(
        self,
        raw: dict,
        current_phase: str,
        candidates: list[CandidateOutput],
        allowed_next: list[str],
        state: "RunState",
        input_artifact: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput]:
        """Normalize and validate one LLM response.

        Returns (result, output) on success.
        Raises WorkflowAbortedError for abort (non-retryable).
        Raises ValueError for any retryable validation failure.

        Extracted from OSRuntime._validate_phase_output; state is now an
        explicit parameter (previously accessed via self._state).
        """
        candidate_map = {c.next_phase: c for c in candidates}

        try:
            result = normalize(raw, allowed_next)
        except ControlIRValidationError as exc:
            self._events.emit("control_ir_validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc
        except NormalizationError as exc:
            self._events.emit("normalization_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        self._events.emit(
            "control_decided",
            phase=current_phase,
            control_type=result.control.type,
            decision=result.control.decision,
            next_phase=result.control.next_phase,
            confidence=result.control.confidence,
            reason=result.control.reason.model_dump(),
            was_normalized=result.was_normalized,
            was_inferred=result.was_inferred,
        )

        if result.control.type == "abort":
            raise WorkflowAbortedError(
                f"LLM aborted workflow at phase '{current_phase}': "
                f"{result.control.reason.summary}"
            )

        if result.control.type == "rollback":
            output = LLMOutput(
                control=result.control,
                artifact={"type": "rollback", "data": {}},
                ops=result.ops,
            )
            return result, output

        matched_candidate = candidate_map[result.control.effective_next_phase]
        normalized = _normalize_artifact(result.artifact, matched_candidate.schema_name)

        try:
            _validate_artifact_structure(normalized, current_phase)
        except ValueError as exc:
            self._events.emit("validation_error", phase=current_phase, error=str(exc))
            raise

        # P7-clean: the OS supplies the generic context dict; only the
        # skill's schema names specific keys.
        validation_context: dict | None = None
        if input_artifact is not None or state.skill_input is not None:
            validation_context = {}
            if input_artifact is not None:
                validation_context["input"] = input_artifact
            if state.skill_input is not None:
                validation_context["skill_input"] = state.skill_input

        norm_data, corrections, errors = validate_artifact_data(
            normalized,
            matched_candidate.artifact_schema,
            strict=self._strict,
            validation_context=validation_context,
        )
        self._events.emit(
            "artifact_validated",
            phase=current_phase,
            artifact_type=normalized.get("type"),
            next_phase=result.control.effective_next_phase,
            was_corrected=bool(corrections),
            corrections=corrections,
            errors=errors,
        )
        if errors:
            error_str = "; ".join(errors)
            self._events.emit("validation_error", phase=current_phase, error=error_str)
            raise ValueError(
                f"Artifact data validation failed for '{normalized.get('type')}': {error_str}"
            )

        try:
            output = LLMOutput(
                control=result.control,
                artifact={**normalized, "data": norm_data},
                ops=result.ops,
            )
        except pydantic.ValidationError as exc:
            msg = f"Invalid ops structure: {exc}"
            self._events.emit("validation_error", phase=current_phase, error=msg)
            raise ValueError(msg) from exc

        try:
            validate_output(output, candidates)
        except ValidationError as exc:
            self._events.emit("validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        return result, output

    # ── Act loop ──────────────────────────────────────────────────────────────

    async def _run_act_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_act_turns: int,
        max_phase_retries: int,
        artifact_path: str | None,
        state: "RunState",
        rollback_context: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """Drive act turns until the LLM emits a decide turn.

        Returns (raw_decide_response, accumulated_prior_attempts).
        """
        control_ir_results: list[dict] = []
        prior_attempts: list[dict[str, str]] = []
        act_turn_count = 0
        first_call = True

        while True:
            if prior_attempts:
                self._events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )

            remaining = max_act_turns - act_turn_count if max_act_turns > 0 else None
            force_decide = remaining is not None and remaining <= 0
            frame = self._build_frame(
                phase, artifact, candidates, output_language,
                control_ir_results=control_ir_results,
                artifact_path=artifact_path,
                remaining_act_turns=remaining,
                force_decide=force_decide,
            )

            # Phase-budget check before each LLM call (moved up from Component B shim)
            await self._check_phase_budget(phase, state)

            raw = await self._llm_caller.call(
                phase, frame, prior_attempts or None,
                rollback_context if first_call else None,
                state,
            )
            first_call = False

            if raw.get("type") != "act":
                return raw, prior_attempts

            act_turn_count += 1
            effective_max_act_turns = state.effective_act_turn_cap(phase, max_act_turns)
            if act_turn_count > effective_max_act_turns:
                if force_decide:
                    act_turn_count -= 1
                    prior_attempts.append({
                        "raw": json.dumps(raw, ensure_ascii=False),
                        "error": (
                            f"You emitted act-turn ops but your act budget is exhausted "
                            f"({effective_max_act_turns}/{effective_max_act_turns} act turns used). "
                            "Do NOT include any ops. Produce the final artifact and transition NOW."
                        ),
                    })
                    if len(prior_attempts) > max_phase_retries:
                        final_msg = (
                            f"Phase '{phase}' failed: LLM refused to produce a decide turn "
                            f"after {len(prior_attempts)} retries with force_decide=True."
                        )
                        self._events.emit("phase_failed", phase=phase,
                                          attempts=len(prior_attempts), final_error=final_msg)
                        raise ValueError(final_msg)
                    continue

                # FP-0005: ask before raising. On approval, extension is
                # recorded on state and the loop continues.
                decision = await handle_limit_exceeded(
                    bus=self._intervention_bus,
                    on_limit=self._safety.on_limit,
                    kind=f"max_act_turns:{phase}",
                    run_id=self._run_id or "",
                    prompt=(
                        f"Phase {phase!r} exceeded max_act_turns "
                        f"({effective_max_act_turns}). Allow more act turns?"
                    ),
                    detail=(
                        f"phase={phase} act_turn_count={act_turn_count} "
                        f"cap={effective_max_act_turns}"
                    ),
                    extension_amount=float(max_act_turns),
                )
                if decision.allow_continue:
                    state.grant_extension(f"max_act_turns:{phase}", decision.extension)
                    self._events.emit(
                        "safety_limit_checkpoint",
                        kind=f"max_act_turns:{phase}",
                        allow_continue=True,
                        reason=decision.reason,
                        extension=decision.extension,
                    )
                    continue
                self._events.emit(
                    "safety_limit_checkpoint",
                    kind=f"max_act_turns:{phase}",
                    allow_continue=False,
                    reason=decision.reason,
                    extension=decision.extension,
                )
                msg = (
                    f"Phase '{phase}' exceeded max act turns ({effective_max_act_turns}). "
                    "The LLM kept emitting act turns without making a decide turn."
                )
                self._events.emit("phase_failed", phase=phase,
                                  attempts=act_turn_count, final_error=msg)
                raise ValueError(msg)

            try:
                act = ActOutput.model_validate(raw)
            except pydantic.ValidationError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self._events.emit("phase_failed", phase=phase,
                                      attempts=len(prior_attempts), final_error=str(exc))
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc
                continue

            phase_def = self._skill.phases.get(phase)
            phase_decl = self._skill.permissions
            allowed_ops = set(phase_def.allowed_ops) if phase_def is not None else None
            ir_results = await self._control_ir_executor.execute(
                act.ops, phase=phase, decl=phase_decl, allowed_ops=allowed_ops,
            )
            control_ir_results = control_ir_results + ir_results
            prior_attempts = []
            self._events.emit(
                "act_executed",
                phase=phase,
                op_count=len(act.ops),
                act_turn=act_turn_count,
                ops=[op.model_dump() for op in act.ops],
                results=ir_results,
            )

    # ── Decide loop with retry ────────────────────────────────────────────────

    async def _run_decide_with_retry(
        self,
        raw: dict,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        prior_attempts: list[dict[str, str]],
        max_phase_retries: int,
        state: "RunState",
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Validate a decide-turn response, retrying on rejection.

        Returns (result, output, retry_count).
        """
        allowed_next = [c.next_phase for c in candidates]

        while True:
            try:
                result, output = self._validate_phase_output(
                    raw, phase, candidates, allowed_next, state, input_artifact=artifact,
                )
                return result, output, len(prior_attempts)
            except WorkflowAbortedError:
                raise
            except ValueError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self._events.emit(
                        "phase_failed", phase=phase,
                        attempts=len(prior_attempts), final_error=str(exc),
                    )
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc

                self._events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )
                frame = self._build_frame(phase, artifact, candidates, output_language)
                # Phase-budget check before each retry LLM call
                await self._check_phase_budget(phase, state)
                raw = await self._llm_caller.call(phase, frame, prior_attempts, None, state)

    # ── Public entry point ────────────────────────────────────────────────────

    async def execute(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_phase_retries: int,
        state: "RunState",
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Drive one phase to completion via act/decide loops with retry.

        Act loop → decide loop → return (result, output, retry_count).
        Phase-budget is checked before each LLM call within act and decide loops.
        """
        phase_def = self._skill.phases[phase]
        max_act_turns = phase_def.max_act_turns if phase_def.max_act_turns > 0 else 10

        raw, prior_attempts = await self._run_act_loop(
            phase, artifact, candidates, output_language,
            max_act_turns, max_phase_retries, artifact_path,
            state,
            rollback_context=rollback_context,
        )
        return await self._run_decide_with_retry(
            raw, phase, artifact, candidates, output_language,
            prior_attempts, max_phase_retries, state,
        )
