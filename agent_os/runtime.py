import json
import pydantic
from .models import App, CandidateOutput, ContextFrame, ExecutionState, PhaseConstraints, LLMOutput
from .events import EventLog
from .workspace import Workspace
from .control_ir_executor import ControlIRExecutor
from .validation import validate_output, ValidationError
from .llm import call_llm
from .normalizer import normalize, NormalizationError, NormalizationResult, ControlIRValidationError
from .artifact_validator import validate_artifact_data


class LoopLimitExceededError(Exception):
    pass


class WorkflowAbortedError(Exception):
    pass


def _schema_type_name(schema: dict) -> str:
    props = schema.get("properties", {})
    const = props.get("type", {}).get("const")
    if const:
        return str(const)
    any_of = schema.get("anyOf", [])
    if any_of:
        names = [s.get("properties", {}).get("type", {}).get("const") for s in any_of]
        named = [str(n) for n in names if n]
        if named:
            return " | ".join(named)
    return "artifact"


def _normalize_artifact(artifact: dict, expected_type: str | None) -> dict:
    _META = frozenset({
        "type", "next_phase", "status", "control_ir",
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
    def __init__(self, app: App, model: str, workspace_dir: str = "./workspace") -> None:
        self.app = app
        self.model = model
        self.events = EventLog()
        self.workspace = Workspace(workspace_dir, self.events)
        self.control_ir_executor = ControlIRExecutor(self.workspace, self.events)
        self._history: list[str] = []
        self._visit_counts: dict[str, int] = {}

    # ── Phase setup ────────────────────────────────────────────────────────────

    def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self.app.graph.max_phase_visits.get(phase_name, 0)
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
        print(f"[phase:{phase_name}] started (visit #{count + 1})")

    def _build_candidates(self, current_phase: str) -> list[CandidateOutput]:
        app = self.app
        allowed = app.graph.transitions.get(current_phase, [])
        can_finish = current_phase in app.graph.can_finish_phases
        candidates: list[CandidateOutput] = [
            CandidateOutput(
                next_phase=phase_name,
                control_type="transition",
                schema_name=_schema_type_name(app.phases[phase_name].input_schema),
                artifact_schema=app.phases[phase_name].input_schema,
                description=app.phases[phase_name].input_description,
            )
            for phase_name in allowed
        ]
        if can_finish or not allowed:
            candidates.append(CandidateOutput(
                next_phase="end",
                control_type="finish",
                schema_name=app.final_output_name or "final_output",
                artifact_schema=app.final_output_schema,
                description=app.final_output_description,
            ))
        return candidates

    def _build_frame(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
    ) -> ContextFrame:
        phase = self.app.phases[current_phase]
        allowed_next = [c.next_phase for c in candidates]
        current_visit = self._visit_counts.get(current_phase, 1)
        total_steps = sum(self._visit_counts.values())
        max_phase_visits = self.app.graph.max_phase_visits.get(current_phase) or None

        frame = ContextFrame(
            current_phase=current_phase,
            current_phase_role=phase.role,
            instructions=phase.instructions,
            input_artifact=artifact,
            execution=ExecutionState(
                path=list(self._history),
                current_visit=current_visit,
                total_steps=total_steps,
            ),
            candidate_outputs=candidates,
            finish_criteria=self.app.finish_criteria if "end" in allowed_next else [],
            constraints=PhaseConstraints(
                max_phase_visits=max_phase_visits,
            ),
            output_language=output_language,
        )

        # Audit: record the exact ContextFrame passed to the LLM for replay/debug
        self.events.emit("context_built", phase=current_phase, frame=frame.model_dump())

        return frame

    # ── Single-attempt validation ───────────────────────────────────────────────

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

        matched_candidate = candidate_map[result.control.effective_next_phase]
        normalized = _normalize_artifact(result.artifact, matched_candidate.schema_name)

        try:
            _validate_artifact_structure(normalized, current_phase)
        except ValueError as exc:
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise

        norm_data, corrections, errors = validate_artifact_data(
            normalized, matched_candidate.artifact_schema
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
                control_ir=result.control_ir,
            )
        except pydantic.ValidationError as exc:
            msg = f"Invalid control_ir structure: {exc}"
            self.events.emit("validation_error", phase=current_phase, error=msg)
            raise ValueError(msg) from exc

        try:
            validate_output(output, candidates)
        except ValidationError as exc:
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        return result, output

    # ── Phase execution with retry ──────────────────────────────────────────────

    def _run_phase(
        self,
        frame: ContextFrame,
        candidates: list[CandidateOutput],
        max_phase_retries: int,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """
        Call LLM and validate, retrying on rejection up to max_phase_retries times.
        Returns (result, output, retry_count).
        """
        current_phase = frame.current_phase
        allowed_next = [c.next_phase for c in candidates]
        prior_attempts: list[dict[str, str]] = []

        for attempt in range(max_phase_retries + 1):
            if attempt > 0:
                last_error = prior_attempts[-1]["error"]
                self.events.emit(
                    "phase_retry", phase=current_phase,
                    attempt=attempt, max_retries=max_phase_retries, error=last_error,
                )
                print(
                    f"[phase:{current_phase}] retry {attempt}/{max_phase_retries} "
                    f"— {last_error[:120]}"
                )

            self.events.emit("llm_called", phase=current_phase, model=self.model)
            print(f"[phase:{current_phase}] calling LLM ({self.model})...")
            raw = call_llm(self.model, frame, prior_attempts=prior_attempts or None)

            try:
                result, output = self._validate_phase_output(raw, current_phase, candidates, allowed_next)
                return result, output, len(prior_attempts)
            except WorkflowAbortedError:
                raise
            except ValueError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if attempt >= max_phase_retries:
                    self.events.emit(
                        "phase_failed", phase=current_phase,
                        attempts=attempt + 1, final_error=str(exc),
                    )
                    raise ValueError(
                        f"Phase '{current_phase}' failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc

        raise RuntimeError("unreachable")  # satisfies type checker

    # ── Logging ────────────────────────────────────────────────────────────────

    @staticmethod
    def _phase_log_suffix(result: NormalizationResult, retry_count: int) -> str:
        norm = (
            " (inferred)" if result.was_inferred
            else f" (normalized from '{result.original_raw_type}')" if result.was_normalized
            else ""
        )
        retries = (
            f" [{retry_count} retr{'y' if retry_count == 1 else 'ies'}]"
            if retry_count else ""
        )
        return f"{norm}{retries}  (confidence={result.control.confidence})"

    # ── Fallback ────────────────────────────────────────────────────────────────

    def _fallback_final_output(self) -> dict:
        for entry in reversed(self.workspace.artifacts):
            art = entry["artifact"]
            if art.get("type") in (self.app.final_output_name, "draft_article", "revised_article"):
                data = art.get("data", {})
                return {
                    "title": data.get("title", ""),
                    "body": data.get("body", ""),
                    "quality_notes": ["Workflow terminated: loop limit exceeded"],
                }
        return {
            "title": "",
            "body": "Workflow terminated before an article was produced.",
            "quality_notes": ["loop_limit_exceeded"],
        }

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(
        self,
        initial_input: dict,
        output_language: str = "ja",
        max_phase_retries: int = 2,
    ) -> dict:
        """
        Execute the workflow from entry_phase to completion.

        max_phase_retries: retries per phase on validation failure (default 2 = 3 total attempts).
        """
        current_phase = self.app.entry_phase
        artifact = initial_input

        try:
            self._enter_phase(current_phase, artifact)

            while True:
                candidates = self._build_candidates(current_phase)
                frame = self._build_frame(current_phase, artifact, candidates, output_language)
                result, output, retry_count = self._run_phase(frame, candidates, max_phase_retries)

                self.control_ir_executor.execute(output.control_ir)
                self.workspace.store_artifact(current_phase, output.artifact)

                self.events.emit(
                    "phase_completed",
                    phase=current_phase,
                    next=output.next_phase,
                    was_normalized=result.was_normalized,
                    was_inferred=result.was_inferred,
                    retries=retry_count,
                    reason=result.control.reason.summary,
                    confidence=result.control.confidence,
                )
                print(f"[phase:{current_phase}] → {output.next_phase}"
                      f"{self._phase_log_suffix(result, retry_count)}")

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
                    return data

                self._history.append(f"{current_phase} → {output.next_phase}")
                current_phase = output.next_phase
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
            print("[os] loop limit reached — returning latest artifact")
            return final_output

        except WorkflowAbortedError as exc:
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
            )
            print(f"[os] workflow aborted — {exc}")
            raise
