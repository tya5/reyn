from .models import App, CandidateOutput, ControlDecision, ContextFrame, LLMOutput
from .events import EventLog
from .workspace import Workspace
from .validation import validate_output, ValidationError
from .llm import call_llm
from .normalizer import normalize, NormalizationError, ControlIRValidationError
from .artifact_validator import validate_artifact_data


class LoopLimitExceededError(Exception):
    pass


class WorkflowAbortedError(Exception):
    pass


def _schema_type_name(schema: dict) -> str:
    """
    Extract artifact type name(s) from a JSON schema.
    Returns "draft_article | revised_article" for anyOf schemas.
    """
    props = schema.get("properties", {})
    const = props.get("type", {}).get("const")
    if const:
        return str(const)
    any_of = schema.get("anyOf", [])
    if any_of:
        names = [
            s.get("properties", {}).get("type", {}).get("const")
            for s in any_of
        ]
        named = [str(n) for n in names if n]
        if named:
            return " | ".join(named)
    return "artifact"


def _normalize_artifact(artifact: dict, expected_type: str | None) -> dict:
    """
    Ensure artifact has {type, data} structure.

    - If artifact already has data dict: remove 'type' contamination from data.
    - If artifact is flat (no data key): wrap as {type, data}.

    expected_type is used to fill artifact.type when it is absent.
    For anyOf types ("a | b"), it is not used as the type value.
    """
    _META = frozenset({
        "type", "next_phase", "status", "control_ir",
        "reason", "confidence", "final_output", "control",
    })

    if isinstance(artifact.get("data"), dict):
        # Proper structure — sanitize type contamination inside data
        cleaned_data = {k: v for k, v in artifact["data"].items() if k != "type"}
        return {**artifact, "data": cleaned_data}
    else:
        # Flat artifact — wrap it into {type, data}
        t = artifact.get("type")
        if t is None and expected_type and "|" not in expected_type:
            t = expected_type
        data = {k: v for k, v in artifact.items() if k not in _META}
        return {"type": t, "data": data}


def _validate_artifact_structure(artifact: dict, context: str) -> None:
    """Raise ValueError if artifact does not have the required {type, data} structure."""
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
        self._history: list[str] = []
        self._visit_counts: dict[str, int] = {}

    def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self.app.graph.max_phase_visits.get(phase_name, 0)
        count = self._visit_counts.get(phase_name, 0)
        if max_visits and count >= max_visits:
            self.events.emit(
                "loop_limit_exceeded",
                phase=phase_name,
                visit_count=count,
                max=max_visits,
            )
            raise LoopLimitExceededError(
                f"Phase '{phase_name}' reached max_phase_visits={max_visits}"
            )
        self._visit_counts[phase_name] = count + 1
        self.events.emit(
            "phase_started",
            phase=phase_name,
            visit_count=count + 1,
            input_artifact_type=artifact.get("type"),
        )
        print(f"[phase:{phase_name}] started (visit #{count + 1})")

    def _build_candidates(self, current_phase: str) -> list[CandidateOutput]:
        """
        Build candidate_outputs for the current phase.

        For each allowed next phase: use that phase's input_schema and input_description.
        schema_name is extracted from the schema's type.const so the LLM knows what
        artifact type to produce.

        If the phase can_finish or has no outgoing transitions: add "end" candidate
        using the app's final_output_schema and final_output_description.
        """
        app = self.app
        allowed = app.graph.transitions.get(current_phase, [])
        can_finish = current_phase in app.graph.can_finish_phases

        candidates: list[CandidateOutput] = []

        for phase_name in allowed:
            phase = app.phases[phase_name]
            candidates.append(CandidateOutput(
                next_phase=phase_name,
                control_type="transition",
                schema_name=_schema_type_name(phase.input_schema),
                artifact_schema=phase.input_schema,
                description=phase.input_description,
            ))

        is_terminal = len(allowed) == 0
        if can_finish or is_terminal:
            candidates.append(CandidateOutput(
                next_phase="end",
                control_type="finish",
                schema_name=app.final_output_name or "final_output",
                artifact_schema=app.final_output_schema,
                description=app.final_output_description,
            ))

        return candidates

    def _fallback_final_output(self) -> dict:
        """Return best available data when loop limit is exceeded."""
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

    def run(self, initial_input: dict, output_language: str = "ja") -> dict:
        current_phase = self.app.entry_phase
        artifact = initial_input

        try:
            self._enter_phase(current_phase, artifact)

            while True:
                phase = self.app.phases[current_phase]
                candidates = self._build_candidates(current_phase)
                allowed_next = [c.next_phase for c in candidates]
                candidate_map = {c.next_phase: c for c in candidates}
                visit = self._visit_counts.get(current_phase, 1)
                max_visit = self.app.graph.max_phase_visits.get(current_phase) or None

                frame = ContextFrame(
                    current_phase=current_phase,
                    current_phase_role=phase.role,
                    instructions=phase.instructions,
                    input_artifact=artifact,
                    history_summary="\n".join(self._history) or "No history yet.",
                    candidate_outputs=candidates,
                    finish_criteria=self.app.finish_criteria if "end" in allowed_next else [],
                    output_language=output_language,
                    current_phase_visit=visit,
                    max_phase_visit=max_visit,
                )

                self.events.emit("llm_called", phase=current_phase, model=self.model)
                print(f"[phase:{current_phase}] calling LLM ({self.model})...")
                raw = call_llm(self.model, frame)

                try:
                    result = normalize(raw, allowed_next)
                except ControlIRValidationError as exc:
                    self.events.emit(
                        "control_ir_validation_error", phase=current_phase, error=str(exc)
                    )
                    raise ValueError(str(exc)) from exc
                except NormalizationError as exc:
                    self.events.emit(
                        "normalization_error", phase=current_phase, error=str(exc)
                    )
                    raise ValueError(str(exc)) from exc

                # Emit control_decided event immediately after normalization
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

                # Handle abort before any artifact processing
                if result.control.type == "abort":
                    raise WorkflowAbortedError(
                        f"LLM aborted workflow at phase '{current_phase}': {result.control.reason}"
                    )

                # Derive effective next_phase from control decision
                effective_next = result.control.effective_next_phase
                matched_candidate = candidate_map[effective_next]

                # Step 1: Normalize {type, data} structure
                normalized = _normalize_artifact(result.artifact, matched_candidate.schema_name)

                # Step 2: Structural check — type and data keys must exist
                try:
                    _validate_artifact_structure(normalized, current_phase)
                except ValueError as exc:
                    self.events.emit(
                        "validation_error", phase=current_phase, error=str(exc)
                    )
                    raise

                # Step 3: Normalize and validate artifact.data contents
                norm_data, corrections, errors = validate_artifact_data(
                    normalized, matched_candidate.artifact_schema
                )
                self.events.emit(
                    "artifact_validated",
                    phase=current_phase,
                    artifact_type=normalized.get("type"),
                    next_phase=effective_next,
                    was_corrected=bool(corrections),
                    corrections=corrections,
                    errors=errors,
                )
                if errors:
                    error_str = "; ".join(errors)
                    self.events.emit(
                        "validation_error", phase=current_phase, error=error_str
                    )
                    raise ValueError(
                        f"Artifact data validation failed for '{normalized.get('type')}': "
                        f"{error_str}"
                    )
                # Replace data with normalized (cleaned, coerced) version
                normalized = {**normalized, "data": norm_data}

                output = LLMOutput(
                    control=result.control,
                    artifact=normalized,
                    control_ir=result.control_ir,
                )

                # Step 4: JSON Schema validation (structural + type backstop)
                try:
                    validate_output(output, candidates)
                except ValidationError as exc:
                    self.events.emit(
                        "validation_error", phase=current_phase, error=str(exc)
                    )
                    raise ValueError(str(exc)) from exc

                self.workspace.execute_control_ir(output.control_ir)
                self.workspace.store_artifact(current_phase, output.artifact)

                suffix = (
                    " (inferred)" if result.was_inferred
                    else f" (normalized from '{result.original_raw_type}')"
                    if result.was_normalized else ""
                )
                conf_str = f"  (confidence={result.control.confidence})"

                if output.next_phase == "end":
                    self._history.append(f"{current_phase} → END")
                    total_phases = sum(self._visit_counts.values())
                    data = output.artifact.get("data", {})
                    final_keys = list(data.keys())

                    self.events.emit(
                        "phase_completed",
                        phase=current_phase,
                        next="end",
                        was_normalized=result.was_normalized,
                        was_inferred=result.was_inferred,
                        reason=result.control.reason,
                        confidence=result.control.confidence,
                    )
                    self.events.emit(
                        "workflow_finished",
                        phase=current_phase,
                        reason=result.control.reason,
                        confidence=result.control.confidence,
                        total_phase_count=total_phases,
                        final_output_keys=final_keys,
                    )
                    print(f"[phase:{current_phase}] → end{suffix}{conf_str}")
                    # Return only the data payload — callers receive the clean flat dict
                    return data

                else:
                    self._history.append(f"{current_phase} → {output.next_phase}")
                    self.events.emit(
                        "phase_completed",
                        phase=current_phase,
                        next=output.next_phase,
                        was_normalized=result.was_normalized,
                        was_inferred=result.was_inferred,
                        reason=result.control.reason,
                        confidence=result.control.confidence,
                    )
                    print(f"[phase:{current_phase}] → {output.next_phase}{suffix}{conf_str}")

                    current_phase = output.next_phase
                    artifact = output.artifact
                    self._enter_phase(current_phase, artifact)

        except LoopLimitExceededError as exc:
            final_output = self._fallback_final_output()
            total_phases = sum(self._visit_counts.values())
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=total_phases,
                final_output_keys=list(final_output.keys()),
            )
            print("[os] loop limit reached — returning latest artifact")
            return final_output

        except WorkflowAbortedError as exc:
            total_phases = sum(self._visit_counts.values())
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=total_phases,
            )
            print(f"[os] workflow aborted — {exc}")
            raise
