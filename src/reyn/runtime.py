from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
import pydantic
from .models import ActOutput, Skill, CandidateOutput, ContextFrame, LLMOutput
from .events import EventLog
from .workspace import Workspace
from .config import LimitsConfig
from .control_ir_executor import ControlIRExecutor
from .validation import validate_output, ValidationError
from .llm import call_llm
from .pricing import TokenUsage, estimate_cost
from .normalizer import normalize, NormalizationError, NormalizationResult, ControlIRValidationError
from .artifact_validator import validate_artifact_data
from .model_resolver import ModelResolver
from .permissions import PermissionResolver
from .context_builder import build_frame
from .skill_node_runner import execute_skill_node
from .preprocessor_executor import PreprocessorExecutor


class LoopLimitExceededError(Exception):
    pass


class PhaseBudgetExceededError(Exception):
    """Raised when a phase exceeds its wall-clock budget (limits.phase.max_wall_seconds)."""
    def __init__(self, phase: str, elapsed: float, budget: float) -> None:
        super().__init__(
            f"Phase '{phase}' exceeded wall-clock budget: {elapsed:.2f}s > {budget:.3g}s"
        )
        self.phase = phase
        self.elapsed = elapsed
        self.budget = budget


class WorkflowAbortedError(Exception):
    pass


@dataclass
class RunResult:
    """Typed return value of OSRuntime.run() and Agent.run()."""
    data: dict[str, Any]
    status: Literal["finished", "loop_limit_exceeded", "phase_budget_exceeded"]
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "finished"


def _normalize_artifact(artifact: dict, expected_type: str | None) -> dict:
    _META = frozenset({
        "type", "next_phase", "status", "ops",
        "reason", "confidence", "final_output", "control",
    })
    if isinstance(artifact.get("data"), dict):
        cleaned_data = {k: v for k, v in artifact["data"].items() if k != "type"}
        return {**artifact, "data": cleaned_data}
    t = artifact.get("type")
    if t is None and expected_type and "|" not in expected_type:
        t = expected_type
    data = {k: v for k, v in artifact.items() if k not in _META}
    return {"type": t, "data": data}


@dataclass
class RollbackState:
    """All rollback-specific bookkeeping for a single OSRuntime run.

    OSRuntime owns the run, this owns the rollback machinery — kept here so
    the four-or-five fields that exist purely to support rollback don't
    pollute OSRuntime's instance namespace.

    Field semantics:
      phase_inputs[phase]  — the artifact the phase was entered with
                             (used to restore on rollback into that phase)
      phase_outputs[phase] — the artifact the phase last produced
                             (used as `rejected_artifact` for the next iteration)
      phase_prev[phase]    — the phase that was the predecessor when this phase
                             was last entered (used to walk back on rollback)
      pending_ctx          — single-shot rollback_ctx for the next _execute_phase
      no_progress_check    — single-shot sentinel: if the rolled-back-into phase
                             re-produces the rejected output, abort
    """
    phase_inputs: dict[str, dict] = field(default_factory=dict)
    phase_outputs: dict[str, dict] = field(default_factory=dict)
    phase_prev: dict[str, str | None] = field(default_factory=dict)
    pending_ctx: dict | None = None
    no_progress_check: dict | None = None

    # ── recording (called by OSRuntime as it advances) ──

    def record_input(self, phase: str, artifact: dict) -> None:
        self.phase_inputs[phase] = artifact

    def record_output(self, phase: str, artifact: dict) -> None:
        self.phase_outputs[phase] = artifact

    def record_predecessor(self, phase: str, prev: str | None) -> None:
        self.phase_prev[phase] = prev

    # ── reading (used to restore on rollback) ──

    def get_input(self, phase: str) -> dict:
        return self.phase_inputs[phase]

    def get_predecessor(self, phase: str) -> str | None:
        return self.phase_prev.get(phase)

    # ── rollback transition ──

    def begin_rollback(self, from_phase: str, to_phase: str, reason: str) -> dict:
        """Set up state for the upcoming re-run of `to_phase`.

        Captures the rollback context (rejected output + reason + caller phase)
        and arms the no-progress sentinel. Returns the rollback context for
        callers that want to log or inspect it; OSRuntime normally consumes it
        via `take_pending_ctx()` on the next iteration.
        """
        rejected = self.phase_outputs.get(to_phase, {})
        ctx = {
            "rejected_artifact": rejected,
            "reason": reason,
            "rollback_from": from_phase,
        }
        self.pending_ctx = ctx
        self.no_progress_check = {
            "phase": to_phase,
            "prev_output_data": rejected.get("data"),
            "rollback_from": from_phase,
        }
        return ctx

    def take_pending_ctx(self) -> dict | None:
        """One-shot read+clear of the rollback context."""
        ctx = self.pending_ctx
        self.pending_ctx = None
        return ctx

    def consume_no_progress(self, phase: str, output_data: Any) -> str | None:
        """Check & clear the no-progress sentinel for `phase`.

        If `phase` is the one we just rolled into and `output_data` matches the
        previously-rejected output, returns the original rollback_from (the
        caller should abort with a no-progress error).

        If `phase` doesn't match the sentinel, leaves it alone — a different
        phase may yet visit this check. If `phase` matches but the output
        differs, clears the sentinel (rollback succeeded; the check has
        served its purpose).
        """
        if self.no_progress_check is None:
            return None
        if self.no_progress_check["phase"] != phase:
            return None
        if output_data == self.no_progress_check["prev_output_data"]:
            rollback_from = self.no_progress_check.get("rollback_from", "?")
            self.no_progress_check = None
            return rollback_from
        self.no_progress_check = None
        return None


def _validate_artifact_structure(artifact: dict, context: str) -> None:
    if "type" not in artifact:
        raise ValueError(f"[{context}] artifact is missing 'type' field")
    if "data" not in artifact:
        raise ValueError(f"[{context}] artifact is missing 'data' field")
    if not isinstance(artifact["data"], dict):
        raise ValueError(
            f"[{context}] artifact['data'] must be a dict, "
            f"got {type(artifact['data']).__name__}"
        )


class OSRuntime:
    def __init__(
        self,
        skill: Skill,
        model: str,
        state_dir: str = ".reyn",
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        user_input_fn: Callable[[str, list[str]], str] | None = None,
        run_id: str | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        limits: LimitsConfig | None = None,
        mcp_servers: dict | None = None,
        python_allowed_modules: list[str] | None = None,
    ) -> None:
        self.skill = skill
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self.strict = strict
        self.run_id = run_id
        self.events = EventLog(subscribers=subscribers)
        self.workspace = Workspace(
            self.events, state_dir=state_dir,
            permission_resolver=permission_resolver,
            skill_name=skill.name,
        )
        self._limits = limits or LimitsConfig()
        self._max_phase_visits = self._limits.phase.max_visits  # 0 = unlimited
        self._max_phase_wall_seconds = self._limits.phase.max_wall_seconds  # 0 = unlimited
        self._llm_timeout = self._limits.llm.timeout
        self._llm_max_retries = self._limits.llm.max_retries
        self._phase_started_at: float | None = None
        self._perm = permission_resolver
        self.control_ir_executor = ControlIRExecutor(
            self.workspace, self.events,
            user_input_fn=user_input_fn,
            shell_allowed=shell_allowed,
            resolver=self._resolver,
            permission_resolver=permission_resolver,
            max_phase_visits=self._max_phase_visits,
            skill_name=skill.name,
            mcp_servers=mcp_servers,
        )
        self._preprocessor = PreprocessorExecutor(
            skill=skill,
            model=self.model,
            events=self.events,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            state_dir=state_dir,
            max_phase_visits=self._max_phase_visits,
            permission_resolver=permission_resolver,
            python_allowed_modules=python_allowed_modules,
        )
        self._history: list[str] = []
        self._visit_counts: dict[str, int] = {}
        self._token_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0
        self._prev_phase: str | None = None          # phase that transitioned to current
        self._rollback = RollbackState()

    # ── Phase setup ────────────────────────────────────────────────────────────

    def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self._max_phase_visits
        count = self._visit_counts.get(phase_name, 0)
        if max_visits and count >= max_visits:
            self.events.emit("loop_limit_exceeded", phase=phase_name, visit_count=count, max=max_visits)
            raise LoopLimitExceededError(
                f"Phase '{phase_name}' reached max_phase_visits={max_visits}"
            )
        self._visit_counts[phase_name] = count + 1
        # Reset wall-clock timer for this visit. Soft budget checks at retry/turn boundaries
        # compare elapsed against limits.phase.max_wall_seconds (0 = unlimited).
        self._phase_started_at = time.monotonic()
        self.events.emit(
            "phase_started", phase=phase_name,
            visit_count=count + 1, input_artifact_type=artifact.get("type"),
        )

    def _check_phase_budget(self, phase_name: str) -> None:
        """Soft wall-clock check. Raises PhaseBudgetExceededError when over budget."""
        budget = self._max_phase_wall_seconds
        if not budget or self._phase_started_at is None:
            return
        elapsed = time.monotonic() - self._phase_started_at
        if elapsed > budget:
            self.events.emit(
                "phase_budget_exceeded",
                phase=phase_name, elapsed=elapsed, budget=budget,
            )
            raise PhaseBudgetExceededError(phase_name, elapsed, budget)

    def _build_candidates(self, current_phase: str) -> list[CandidateOutput]:
        skill = self.skill
        allowed = skill.graph.transitions.get(current_phase, [])
        can_finish = current_phase in skill.graph.can_finish_phases
        candidates: list[CandidateOutput] = []
        for phase_name in allowed:
            if phase_name in skill.graph.skill_nodes:
                node_spec = skill.graph.skill_nodes[phase_name]
                candidates.append(CandidateOutput(
                    next_phase=phase_name,
                    control_type="transition",
                    schema_name=node_spec.entry_input_schema_name,
                    artifact_schema=node_spec.entry_input_schema,
                    description=node_spec.entry_input_description,
                ))
            else:
                p = skill.phases[phase_name]
                candidates.append(CandidateOutput(
                    next_phase=phase_name,
                    control_type="transition",
                    schema_name=p.input_schema_name,
                    artifact_schema=p.input_schema,
                    description=p.input_description,
                ))
        if can_finish or not allowed:
            candidates.append(CandidateOutput(
                next_phase="end",
                control_type="finish",
                schema_name=skill.final_output_name or "final_output",
                artifact_schema=skill.final_output_schema,
                description=skill.final_output_description,
            ))
        return candidates

    def _effective_model(self, phase_name: str) -> str:
        phase = self.skill.phases.get(phase_name)
        return phase.model_class if phase and phase.model_class else self.model

    def _build_frame(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
        control_ir_results: list[dict] | None = None,
        artifact_path: str | None = None,
    ) -> ContextFrame:
        effective_model = self._effective_model(current_phase)
        phase_def = self.skill.phases[current_phase]
        allowed = set(phase_def.allowed_ops)
        all_ops = self.control_ir_executor.available_ops()
        filtered_ops = [op for op in all_ops if op.kind in allowed]
        return build_frame(
            phase_name=current_phase,
            phase=phase_def,
            artifact=artifact,
            candidates=candidates,
            output_language=output_language,
            history=self._history,
            visit_counts=self._visit_counts,
            finish_criteria=self.skill.finish_criteria,
            max_phase_visits=self._max_phase_visits or None,
            available_ops=filtered_ops,
            effective_model=effective_model,
            model_resolved=self._resolver.resolve(effective_model),
            events=self.events,
            control_ir_results=control_ir_results,
            artifact_path=artifact_path,
        )

    # ── Single-attempt validation ──────────────────────────────────────────────

    def _validate_phase_output(
        self,
        raw: dict,
        current_phase: str,
        candidates: list[CandidateOutput],
        allowed_next: list[str],
    ) -> tuple[NormalizationResult, LLMOutput]:
        """
        Normalize and validate one LLM response.
        Returns (result, output) on success.
        Raises WorkflowAbortedError for abort (non-retryable).
        Raises ValueError for any retryable validation failure.
        """
        candidate_map = {c.next_phase: c for c in candidates}

        try:
            result = normalize(raw, allowed_next)
        except ControlIRValidationError as exc:
            self.events.emit("control_ir_validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc
        except NormalizationError as exc:
            self.events.emit("normalization_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        self.events.emit(
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
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise

        norm_data, corrections, errors = validate_artifact_data(
            normalized, matched_candidate.artifact_schema, strict=self.strict
        )
        self.events.emit(
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
            self.events.emit("validation_error", phase=current_phase, error=error_str)
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
            self.events.emit("validation_error", phase=current_phase, error=msg)
            raise ValueError(msg) from exc

        try:
            validate_output(output, candidates)
        except ValidationError as exc:
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        return result, output

    # ── Phase execution with act/decide loop and retry ─────────────────────────

    async def _call_llm_and_record(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict[str, str]] | None,
        rollback_context: dict | None = None,
    ) -> dict:
        self._check_phase_budget(phase)
        resolved_model = self._resolver.resolve(self._effective_model(phase))
        self.events.emit("llm_called", phase=phase, model=resolved_model)
        llm_result = await call_llm(
            resolved_model, frame,
            prior_attempts=prior_attempts or None,
            rollback_context=rollback_context,
            timeout=self._llm_timeout,
            max_retries=self._llm_max_retries,
        )
        raw = llm_result.data
        cost_usd: float | None = None
        pricing_snapshot: dict | None = None
        if llm_result.usage:
            self._token_usage += llm_result.usage
            cost_usd, pricing_snapshot = estimate_cost(resolved_model, llm_result.usage)
            if cost_usd is not None:
                self._total_cost_usd += cost_usd
        self.events.emit(
            "llm_response_received",
            phase=phase,
            response_type=raw.get("type"),
            raw=raw,
            prompt_tokens=llm_result.usage.prompt_tokens if llm_result.usage else None,
            completion_tokens=llm_result.usage.completion_tokens if llm_result.usage else None,
            cost_usd=cost_usd,
            pricing_snapshot=pricing_snapshot,
        )
        return raw

    async def _run_act_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
        max_act_turns: int,
        max_phase_retries: int,
        artifact_path: str | None,
        rollback_context: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """
        Drive act turns until the LLM emits a decide turn.
        Returns (raw_decide_response, accumulated_prior_attempts).
        """
        control_ir_results: list[dict] = []
        prior_attempts: list[dict[str, str]] = []
        act_turn_count = 0
        first_call = True

        while True:
            if prior_attempts:
                self.events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )

            frame = self._build_frame(
                phase, artifact, candidates, output_language,
                control_ir_results=control_ir_results,
                artifact_path=artifact_path,
            )
            # Pass rollback_context only on the first LLM call; subsequent calls already have context
            raw = await self._call_llm_and_record(
                phase, frame, prior_attempts or None,
                rollback_context=rollback_context if first_call else None,
            )
            first_call = False

            if raw.get("type") != "act":
                return raw, prior_attempts

            act_turn_count += 1
            if act_turn_count > max_act_turns:
                msg = (
                    f"Phase '{phase}' exceeded max act turns ({max_act_turns}). "
                    "The LLM kept emitting act turns without making a decide turn."
                )
                self.events.emit("phase_failed", phase=phase,
                                 attempts=act_turn_count, final_error=msg)
                raise ValueError(msg)

            try:
                act = ActOutput.model_validate(raw)
            except pydantic.ValidationError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self.events.emit("phase_failed", phase=phase,
                                     attempts=len(prior_attempts), final_error=str(exc))
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc
                continue

            phase_def = self.skill.phases.get(phase)
            phase_decl = phase_def.permissions if phase_def is not None else None
            allowed_ops = set(phase_def.allowed_ops) if phase_def is not None else None
            ir_results = await self.control_ir_executor.execute(
                act.ops, phase=phase, decl=phase_decl, allowed_ops=allowed_ops,
            )
            control_ir_results = ir_results
            prior_attempts = []
            self.events.emit(
                "act_executed",
                phase=phase,
                op_count=len(act.ops),
                act_turn=act_turn_count,
                ops=[op.model_dump() for op in act.ops],
                results=ir_results,
            )

    async def _run_decide_with_retry(
        self,
        raw: dict,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
        prior_attempts: list[dict[str, str]],
        max_phase_retries: int,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """
        Validate a decide-turn response, retrying on rejection.
        Returns (result, output, retry_count).
        """
        allowed_next = [c.next_phase for c in candidates]

        while True:
            try:
                result, output = self._validate_phase_output(raw, phase, candidates, allowed_next)
                return result, output, len(prior_attempts)
            except WorkflowAbortedError:
                raise
            except ValueError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self.events.emit(
                        "phase_failed", phase=phase,
                        attempts=len(prior_attempts), final_error=str(exc),
                    )
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc

                self.events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )
                frame = self._build_frame(phase, artifact, candidates, output_language)
                # rollback_context already injected in act loop's first call; retries don't repeat it
                raw = await self._call_llm_and_record(phase, frame, prior_attempts)

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
        max_phase_retries: int,
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Drive one phase to completion via act/decide loops with retry."""
        phase_def = self.skill.phases[current_phase]
        max_act_turns = phase_def.max_act_turns if phase_def.max_act_turns > 0 else 10

        raw, prior_attempts = await self._run_act_loop(
            current_phase, artifact, candidates, output_language,
            max_act_turns, max_phase_retries, artifact_path,
            rollback_context=rollback_context,
        )
        return await self._run_decide_with_retry(
            raw, current_phase, artifact, candidates, output_language, prior_attempts, max_phase_retries,
        )

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_final_output(self) -> dict:
        for entry in reversed(self.workspace.artifacts):
            art = entry["artifact"]
            if art.get("type") == self.skill.final_output_name:
                return art.get("data", {})
        if self.workspace.artifacts:
            return self.workspace.artifacts[-1]["artifact"].get("data", {})
        return {}

    # ── App-node dispatch ──────────────────────────────────────────────────────

    async def _run_skill_node(
        self,
        node_id: str,
        input_artifact: dict,
        target_schema: dict,
        target_type: str,
        output_language: str,
    ) -> dict:
        node_spec = self.skill.graph.skill_nodes[node_id]
        adapted, usage = await execute_skill_node(
            node_id=node_id,
            node_spec=node_spec,
            input_artifact=input_artifact,
            target_schema=target_schema,
            target_type=target_type,
            output_language=output_language,
            parent_state_dir=self.workspace.state_dir,
            model=self.model,
            strict=self.strict,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            events=self.events,
            limits=self._limits,
        )
        self._token_usage += usage
        return adapted

    # ── Rollback dispatch ──────────────────────────────────────────────────────

    def _handle_rollback(
        self, current_phase: str, reason_summary: str,
    ) -> tuple[str, dict, str | None]:
        """Process a rollback decision.

        Returns (target_phase, target_input_artifact, target_predecessor).
        Raises WorkflowAbortedError if there is no previous phase to roll
        back to (e.g. the very first phase emitted rollback).
        """
        target = self._prev_phase
        if target is None:
            raise WorkflowAbortedError(
                f"Phase '{current_phase}' emitted rollback but there is no previous phase."
            )
        self.events.emit(
            "phase_rollback",
            rollback_from=current_phase,
            rollback_to=target,
            reason=reason_summary,
        )
        self._rollback.begin_rollback(current_phase, target, reason_summary)
        self._history.append(f"{current_phase} → rollback → {target}")
        return target, self._rollback.get_input(target), self._rollback.get_predecessor(target)

    # ── Workflow termination ──────────────────────────────────────────────────

    def _finish_workflow(
        self, phase: str, data: dict, reason: str, confidence: float,
    ) -> RunResult:
        """Single source of truth for "the workflow ended cleanly".

        Both the normal end-of-graph path and the skill_node terminal path
        go through here so observers see consistent event shape and the
        RunResult is constructed identically.
        """
        self.events.emit(
            "workflow_finished",
            phase=phase,
            reason=reason,
            confidence=confidence,
            total_phase_count=sum(self._visit_counts.values()),
            final_output_keys=list(data.keys()),
        )
        return RunResult(
            data=data, status="finished",
            token_usage=self._token_usage,
            cost_usd=self._total_cost_usd or None,
        )

    # ── Skill-node dispatch (transition to a sub-skill node) ───────────────────

    async def _apply_skill_node(
        self,
        node_id: str,
        current_phase: str,
        output_artifact: dict,
        output_language: str,
    ) -> "RunResult | tuple[str, dict]":
        """Run a skill_node and decide whether the workflow ends here.

        Returns either:
          - a RunResult, when this node is terminal (no post-nodes); the
            caller should propagate it as the workflow's result, or
          - (next_after, adapted_artifact), when execution should continue
            into `next_after` with the LLM-adapted artifact as input.
        """
        post_nodes = self.skill.graph.transitions.get(node_id, [])
        if not post_nodes:
            adapted = await self._run_skill_node(
                node_id, output_artifact,
                self.skill.final_output_schema, self.skill.final_output_name,
                output_language,
            )
            data = adapted.get("data", {})
            self._history.append(f"{current_phase} → {node_id} → END")
            return self._finish_workflow(
                phase=node_id,
                data=data,
                reason="app node produced final output",
                confidence=1.0,
            )
        next_after = post_nodes[0]
        next_phase_obj = self.skill.phases[next_after]
        adapted = await self._run_skill_node(
            node_id, output_artifact,
            next_phase_obj.input_schema, next_phase_obj.input_schema_name,
            output_language,
        )
        self._history.append(f"{current_phase} → {node_id} → {next_after}")
        return next_after, adapted

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(
        self,
        initial_input: dict,
        output_language: str = "ja",
        max_phase_retries: int = 2,
    ) -> RunResult:
        """
        Execute the workflow from entry_phase to completion.

        max_phase_retries: retries per phase on validation failure (default 2 = 3 total attempts).
        Returns RunResult with status="finished" or status="loop_limit_exceeded".
        Raises WorkflowAbortedError on unrecoverable LLM abort.
        """
        if self._perm:
            self._perm.startup_guard(self.skill, self.skill.name)

        current_phase = self.skill.entry_phase
        artifact = initial_input

        self.events.emit(
            "workflow_started",
            run_id=self.run_id,
            skill=self.skill.name,
            entry_phase=self.skill.entry_phase,
            input_type=artifact.get("type"),
            default_model=self._resolver.resolve(self.model),
        )

        artifact_path: str | None = self.workspace.store_artifact(
            "_input", artifact, skill_name=self.skill.name, visit=1
        )

        try:
            self._enter_phase(current_phase, artifact)

            while True:
                rollback_context = self._rollback.take_pending_ctx()

                # Store the pre-preprocessor artifact for rollback.
                # On rollback, the preprocessor re-runs deterministically from this snapshot —
                # semantically correct, but costly for heavy chains (iterate × run_app).
                # If eval rollback causes N-item re-evaluation, revisit caching here (Phase 5+).
                self._rollback.record_input(current_phase, artifact)

                candidates = self._build_candidates(current_phase)

                # Run preprocessor (deterministic enrichment) before handing artifact to LLM
                phase_def = self.skill.phases[current_phase]
                if phase_def.preprocessor:
                    enriched_artifact, pre_usage = await self._preprocessor.run(
                        phase_def, artifact, output_language
                    )
                    self._token_usage += pre_usage
                    # Update artifact_path to the enriched file so maybe_ref_artifact
                    # references the correct (post-preprocessor) artifact when it is large.
                    artifact_path = self.workspace.store_artifact(
                        current_phase + "_preprocessed", enriched_artifact,
                        skill_name=self.skill.name,
                        visit=self._visit_counts.get(current_phase, 1),
                    )
                else:
                    enriched_artifact = artifact

                result, output, retry_count = await self._execute_phase(
                    current_phase, enriched_artifact, candidates, output_language, max_phase_retries,
                    artifact_path=artifact_path,
                    rollback_context=rollback_context,
                )

                current_def = self.skill.phases.get(current_phase)
                current_decl = current_def.permissions if current_def is not None else None
                current_allowed = set(current_def.allowed_ops) if current_def is not None else None
                decide_results = await self.control_ir_executor.execute(
                    output.ops, phase=current_phase, decl=current_decl,
                    allowed_ops=current_allowed,
                )
                if decide_results:
                    self.events.emit(
                        "decide_ops_executed",
                        phase=current_phase,
                        op_count=len(decide_results),
                        ops=[op.model_dump() for op in output.ops],
                        results=decide_results,
                    )

                # Handle rollback before storing artifact or emitting phase_completed
                if result.control.type == "rollback":
                    current_phase, artifact, self._prev_phase = self._handle_rollback(
                        current_phase, result.control.reason.summary,
                    )
                    artifact_path = None
                    self._enter_phase(current_phase, artifact)
                    continue

                # No-progress detection: if this phase was just re-run after a rollback
                # and produced an output structurally identical to the rejected one, abort.
                rollback_from = self._rollback.consume_no_progress(
                    current_phase, output.artifact.get("data"),
                )
                if rollback_from is not None:
                    self.events.emit(
                        "phase_no_progress",
                        phase=current_phase,
                        rollback_from=rollback_from,
                    )
                    raise WorkflowAbortedError(
                        f"Phase '{current_phase}' produced an output identical to the one "
                        f"rejected by '{rollback_from}'. The rollback feedback did not lead "
                        f"to any change — aborting to prevent a wasteful loop."
                    )

                self._rollback.record_output(current_phase, output.artifact)

                artifact_path = self.workspace.store_artifact(
                    current_phase, output.artifact,
                    skill_name=self.skill.name,
                    visit=self._visit_counts.get(current_phase, 1),
                )

                self.events.emit(
                    "phase_completed",
                    phase=current_phase,
                    next=output.next_phase,
                    was_normalized=result.was_normalized,
                    was_inferred=result.was_inferred,
                    retries=retry_count,
                    reason=result.control.reason.summary,
                    confidence=result.control.confidence,
                    artifact_path=artifact_path,
                )

                if output.next_phase == "end":
                    data = output.artifact.get("data", {})
                    self._history.append(f"{current_phase} → END")
                    return self._finish_workflow(
                        phase=current_phase,
                        data=data,
                        reason=result.control.reason.summary,
                        confidence=result.control.confidence,
                    )

                next_node = output.next_phase
                if next_node in self.skill.graph.skill_nodes:
                    outcome = await self._apply_skill_node(
                        next_node, current_phase, output.artifact, output_language,
                    )
                    if isinstance(outcome, RunResult):
                        return outcome
                    next_after, adapted = outcome
                    self._prev_phase = current_phase
                    self._rollback.record_predecessor(next_after, current_phase)
                    current_phase = next_after
                    artifact = adapted
                else:
                    self._history.append(f"{current_phase} → {next_node}")
                    self._prev_phase = current_phase
                    self._rollback.record_predecessor(next_node, current_phase)
                    current_phase = next_node
                    artifact = output.artifact
                self._enter_phase(current_phase, artifact)

        except LoopLimitExceededError as exc:
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(data=final_output, status="loop_limit_exceeded", token_usage=self._token_usage, cost_usd=self._total_cost_usd or None)

        except PhaseBudgetExceededError as exc:
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(data=final_output, status="phase_budget_exceeded", token_usage=self._token_usage, cost_usd=self._total_cost_usd or None)

        except WorkflowAbortedError as exc:
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
            )
            raise
