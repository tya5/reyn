import jsonschema
from reyn.schemas.models import LLMOutput, CandidateOutput
from reyn.workspace.artifact_validator import extract_data_schema


class ValidationError(Exception):
    pass


def validate_output(
    output: LLMOutput,
    candidates: list[CandidateOutput],
) -> CandidateOutput:
    """
    Validate LLM output against the candidate outputs for the current phase.
    Returns the matched CandidateOutput.
    Raises ValidationError on control structure issues or artifact schema mismatch.

    Note: abort type and ControlIR strict validation are handled before this is called.
    """
    ctrl = output.control

    # Validate confidence range (backstop — ControlIRValidationError catches this for new format)
    if not (0.0 <= ctrl.confidence <= 1.0):
        raise ValidationError(
            f"control.confidence {ctrl.confidence} is out of range [0.0, 1.0]"
        )

    # Validate next_phase / control type consistency
    if ctrl.type == "transition" and not ctrl.next_phase:
        raise ValidationError("control.type='transition' requires a non-empty next_phase")
    if ctrl.type == "finish" and ctrl.next_phase is not None:
        raise ValidationError("control.type='finish' must have next_phase=null")
    if ctrl.type == "finish" and ctrl.decision != "finish":
        raise ValidationError(
            f"control.type='finish' requires control.decision='finish', got {ctrl.decision!r}"
        )
    # Validate effective next_phase against candidates
    effective = output.next_phase
    candidate_map = {c.next_phase: c for c in candidates}
    if effective not in candidate_map:
        allowed = list(candidate_map.keys())
        raise ValidationError(
            f"Effective next_phase '{effective}' is not a valid candidate. "
            f"Allowed: {allowed}"
        )

    candidate = candidate_map[effective]

    # Validate artifact data against the candidate's data schema.
    # Always validate artifact["data"] against the extracted data schema, because
    # flat (wrapped=false) candidate schemas describe the data fields directly and
    # must not be applied to the {type, data} wrapper.
    artifact_type = output.artifact.get("type", "")
    data_schema = extract_data_schema(candidate.artifact_schema, artifact_type)
    try:
        jsonschema.validate(instance=output.artifact.get("data", {}), schema=data_schema)
    except jsonschema.ValidationError as e:
        raise ValidationError(
            f"Artifact does not match schema for '{effective}' "
            f"(schema_name='{candidate.schema_name}'): {e.message}"
        )

    return candidate
