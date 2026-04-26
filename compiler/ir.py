from dataclasses import dataclass, field


@dataclass
class FieldDef:
    name: str
    type_str: str      # primitive alias (string | number | integer | boolean | string[] | ...)
    optional: bool = False  # True when declared as "field?: type"
    schema: dict | None = None  # inline JSON Schema — takes priority over type_str when set


@dataclass
class ArtifactDef:
    name: str
    fields: list[FieldDef]
    # False for entry-phase raw inputs (no {type,data} wrapper in JSON Schema)
    wrapped: bool = True


@dataclass
class PhaseDef:
    name: str
    inputs: list[str]           # artifact names
    input_description: str      # free-text description for candidate_outputs
    role: str | None
    can_finish: bool
    instructions: str


@dataclass
class AppDef:
    name: str
    entry: str
    edges: list[tuple[str, str]]        # (from_phase, to_phase)
    final_output: str                    # artifact name for final_output_schema
    final_output_description: str
    finish_criteria: list[str]
    max_phase_visits: dict[str, int]
