from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Callable, Literal
import pydantic
from .models import ActOutput, App, CandidateOutput, ContextFrame, LLMOutput
from .events import EventLog
from .workspace import Workspace
from .control_ir_executor import ControlIRExecutor
from .validation import validate_output, ValidationError
from .llm import call_llm
from .pricing import TokenUsage
from .normalizer import normalize, NormalizationError, NormalizationResult, ControlIRValidationError
from .artifact_validator import validate_artifact_data
from .model_resolver import ModelResolver
from .permissions import PermissionResolver
from .context_builder import build_frame
from .app_node_runner import execute_app_node
from .preprocessor_executor import PreprocessorExecutor


class LoopLimitExceededError(Exception):
    pass


class WorkflowAbortedError(Exception):
    pass


@dataclass
class RunResult:
    """Typed return value of OSRuntime.run() and Agent.run()."""
    data: dict[str, Any]
    status: Literal["finished", "loop_limit_exceeded"]
    token_usage: TokenUsage | None = None

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
        app: App,
        model: str,
        state_dir: str = ".reyn",
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        user_input_fn: Callable[[str, list[str]], str] | None = None,
        run_id: str | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        max_phase_visits: int = 25,
    ) -> None:
        self.app = app
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self.strict = strict
        self.run_id = run_id
        self.events = EventLog(subscribers=subscribers)
        self.workspace = Workspace(self.events, state_dir=state_dir)
        self._max_phase_visits = max_phase_visits  # 0 = unlimited
        self.control_ir_executor = ControlIRExecutor(
            self.workspace, self.events,
            user_input_fn=user_input_fn,
            shell_allowed=shell_allowed,
            resolver=self._resolver,
            permission_resolver=permission_resolver,
            max_phase_visits=max_phase_visits,
        )
        self._preprocessor = PreprocessorExecutor(
            app=app,
            model=self.model,
            events=self.events,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            state_dir=state_dir,
            max_phase_visits=max_phase_visits,
        )
        self._history: list[str] = []
        self._visit_counts: dict[str, int] = {}
        self._token_usage: TokenUsage = TokenUsage()
        self._prev_phase: str | None = None          # phase that transitioned to current
        self._phase_inputs: dict[str, dict] = {}     # phase -> last input artifact
        self._phase_outputs: dict[str, dict] = {}    # phase -> last output artifact
        self._phase_prev: dict[str, str | None] = {} # phase -> its predecessor at entry time
        self._pending_rollback_ctx: dict | None = None  # set when rollback is triggered
        # No-progress detection: when a phase is rolled back into, remember the
        # output it produced just before the rollback. If the next output is
        # structurally identical, abort — the LLM is not making progress.
        self._no_progress_check: dict | None = None

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
        self.events.emit(
            "phase_started", phase=phase_name,
            visit_count=count + 1, input_artifact_type=artifact.get("type"),
        )

    def _build_candidates(self, current_phase: str) -> list[CandidateOutput]:
        app = self.app
        allowed = app.graph.transitions.get(current_phase, [])
        can_finish = current_phase in app.graph.can_finish_phases
        candidates: list[CandidateOutput] = []
        for phase_name in allowed:
            if phase_name in app.graph.app_nodes:
                node_spec = app.graph.app_nodes[phase_name]
                candidates.append(CandidateOutput(
                    next_phase=phase_name,
                    control_type="transition",
                    schema_name=node_spec.entry_input_schema_name,
                    artifact_schema=node_spec.entry_input_schema,
                    description=node_spec.entry_input_description,
                ))
            else:
                p = app.phases[phase_name]
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
                schema_name=app.final_output_name or "final_output",
                artifact_schema=app.final_output_schema,
                description=app.final_output_description,
            ))
        return candidates

    def _effective_model(self, phase_name: str) -> str:
        phase = self.app.phases.get(phase_name)
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
        return build_frame(
            phase_name=current_phase,
            phase=self.app.phases[current_phase],
            artifact=artifact,
            candidates=candidates,
            output_language=output_language,
            history=self._history,
            visit_counts=self._visit_counts,
            finish_criteria=self.app.finish_criteria,
            max_phase_visits=self._max_phase_visits or None,
            available_ops=self.control_ir_executor.available_ops(),
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

    def _call_llm_and_record(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict[str, str]] | None,
        rollback_context: dict | None = None,
    ) -> dict:
        resolved_model = self._resolver.resolve(self._effective_model(phase))
        self.events.emit("llm_called", phase=phase, model=resolved_model)
        llm_result = call_llm(
            resolved_model, frame,
            prior_attempts=prior_attempts or None,
            rollback_context=rollback_context,
        )
        raw = llm_result.data
        if llm_result.usage:
            self._token_usage += llm_result.usage
        self.events.emit(
            "llm_response_received",
            phase=phase,
            response_type=raw.get("type"),
            raw=raw,
            prompt_tokens=llm_result.usage.prompt_tokens if llm_result.usage else None,
            completion_tokens=llm_result.usage.completion_tokens if llm_result.usage else None,
        )
        return raw

    def _run_act_loop(
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
            raw = self._call_llm_and_record(
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

            phase_decl = self.app.phases[phase].permissions if phase in self.app.phases else None
            ir_results = self.control_ir_executor.execute(act.ops, phase=phase, decl=phase_decl)
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

    def _run_decide_with_retry(
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
                raw = self._call_llm_and_record(phase, frame, prior_attempts)

    def _execute_phase(
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
        phase_def = self.app.phases[current_phase]
        max_act_turns = phase_def.max_act_turns if phase_def.max_act_turns > 0 else 10

        raw, prior_attempts = self._run_act_loop(
            current_phase, artifact, candidates, output_language,
            max_act_turns, max_phase_retries, artifact_path,
            rollback_context=rollback_context,
        )
        return self._run_decide_with_retry(
            raw, current_phase, artifact, candidates, output_language, prior_attempts, max_phase_retries,
        )

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_final_output(self) -> dict:
        for entry in reversed(self.workspace.artifacts):
            art = entry["artifact"]
            if art.get("type") == self.app.final_output_name:
                return art.get("data", {})
        if self.workspace.artifacts:
            return self.workspace.artifacts[-1]["artifact"].get("data", {})
        return {}

    # ── App-node dispatch ──────────────────────────────────────────────────────

    def _run_app_node(
        self,
        node_id: str,
        input_artifact: dict,
        target_schema: dict,
        target_type: str,
        output_language: str,
    ) -> dict:
        node_spec = self.app.graph.app_nodes[node_id]
        adapted, usage = execute_app_node(
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
        )
        self._token_usage += usage
        return adapted

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(
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
        current_phase = self.app.entry_phase
        artifact = initial_input

        self.events.emit(
            "workflow_started",
            run_id=self.run_id,
            app=self.app.name,
            entry_phase=self.app.entry_phase,
            input_type=artifact.get("type"),
            default_model=self._resolver.resolve(self.model),
        )

        artifact_path: str | None = self.workspace.store_artifact(
            "_input", artifact, app_name=self.app.name, visit=1
        )

        try:
            self._enter_phase(current_phase, artifact)

            while True:
                rollback_context = self._pending_rollback_ctx
                self._pending_rollback_ctx = None

                # Store the pre-preprocessor artifact for rollback.
                # On rollback, the preprocessor re-runs deterministically from this snapshot —
                # semantically correct, but costly for heavy chains (iterate × run_app).
                # If eval rollback causes N-item re-evaluation, revisit caching here (Phase 5+).
                self._phase_inputs[current_phase] = artifact

                candidates = self._build_candidates(current_phase)

                # Run preprocessor (deterministic enrichment) before handing artifact to LLM
                phase_def = self.app.phases[current_phase]
                if phase_def.preprocessor:
                    enriched_artifact, pre_usage = self._preprocessor.run(
                        phase_def, artifact, output_language
                    )
                    self._token_usage += pre_usage
                    # Update artifact_path to the enriched file so maybe_ref_artifact
                    # references the correct (post-preprocessor) artifact when it is large.
                    artifact_path = self.workspace.store_artifact(
                        current_phase + "_preprocessed", enriched_artifact,
                        app_name=self.app.name,
                        visit=self._visit_counts.get(current_phase, 1),
                    )
                else:
                    enriched_artifact = artifact

                result, output, retry_count = self._execute_phase(
                    current_phase, enriched_artifact, candidates, output_language, max_phase_retries,
                    artifact_path=artifact_path,
                    rollback_context=rollback_context,
                )

                current_decl = self.app.phases[current_phase].permissions if current_phase in self.app.phases else None
                decide_results = self.control_ir_executor.execute(output.ops, phase=current_phase, decl=current_decl)
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
                    if self._prev_phase is None:
                        raise WorkflowAbortedError(
                            f"Phase '{current_phase}' emitted rollback but there is no previous phase."
                        )
                    target_phase = self._prev_phase
                    self.events.emit(
                        "phase_rollback",
                        rollback_from=current_phase,
                        rollback_to=target_phase,
                        reason=result.control.reason.summary,
                    )
                    rejected_target_output = self._phase_outputs.get(target_phase, {})
                    self._pending_rollback_ctx = {
                        "rejected_artifact": rejected_target_output,
                        "reason": result.control.reason.summary,
                        "rollback_from": current_phase,
                    }
                    self._no_progress_check = {
                        "phase": target_phase,
                        "prev_output_data": rejected_target_output.get("data"),
                        "rollback_from": current_phase,
                    }
                    self._history.append(f"{current_phase} → rollback → {target_phase}")
                    current_phase = target_phase
                    artifact = self._phase_inputs[target_phase]
                    artifact_path = None
                    self._prev_phase = self._phase_prev.get(target_phase)
                    self._enter_phase(current_phase, artifact)
                    continue

                # No-progress detection: if this phase was just re-run after a rollback
                # and produced an output structurally identical to the rejected one, abort.
                if (
                    self._no_progress_check is not None
                    and self._no_progress_check["phase"] == current_phase
                ):
                    new_data = output.artifact.get("data")
                    if new_data == self._no_progress_check["prev_output_data"]:
                        rollback_from = self._no_progress_check.get("rollback_from", "?")
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
                    self._no_progress_check = None

                self._phase_outputs[current_phase] = output.artifact

                artifact_path = self.workspace.store_artifact(
                    current_phase, output.artifact,
                    app_name=self.app.name,
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
                    self.events.emit(
                        "workflow_finished",
                        phase=current_phase,
                        reason=result.control.reason.summary,
                        confidence=result.control.confidence,
                        total_phase_count=sum(self._visit_counts.values()),
                        final_output_keys=list(data.keys()),
                    )
                    return RunResult(data=data, status="finished", token_usage=self._token_usage)

                next_node = output.next_phase
                if next_node in self.app.graph.app_nodes:
                    post_nodes = self.app.graph.transitions.get(next_node, [])
                    if not post_nodes:
                        adapted = self._run_app_node(
                            next_node, output.artifact,
                            self.app.final_output_schema, self.app.final_output_name,
                            output_language,
                        )
                        data = adapted.get("data", {})
                        self._history.append(f"{current_phase} → {next_node} → END")
                        self.events.emit(
                            "workflow_finished",
                            phase=next_node,
                            reason="app node produced final output",
                            confidence=1.0,
                            total_phase_count=sum(self._visit_counts.values()),
                            final_output_keys=list(data.keys()),
                        )
                        return RunResult(data=data, status="finished", token_usage=self._token_usage)
                    next_after = post_nodes[0]
                    next_phase_obj = self.app.phases[next_after]
                    adapted = self._run_app_node(
                        next_node, output.artifact,
                        next_phase_obj.input_schema, next_phase_obj.input_schema_name,
                        output_language,
                    )
                    self._history.append(f"{current_phase} → {next_node} → {next_after}")
                    self._prev_phase = current_phase
                    self._phase_prev[next_after] = current_phase
                    current_phase = next_after
                    artifact = adapted
                else:
                    self._history.append(f"{current_phase} → {next_node}")
                    self._prev_phase = current_phase
                    self._phase_prev[next_node] = current_phase
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
            return RunResult(data=final_output, status="loop_limit_exceeded", token_usage=self._token_usage)

        except WorkflowAbortedError as exc:
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
            )
            raise
