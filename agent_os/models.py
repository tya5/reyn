from __future__ import annotations
from datetime import datetime
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, Field, model_validator


class Phase(BaseModel):
    name: str
    role: str | None = None
    input_schema: dict[str, Any]
    input_description: str = ""
    instructions: str


class AppGraph(BaseModel):
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    can_finish_phases: list[str] = Field(default_factory=list)
    # phase_name -> max allowed visits per run (0 = unlimited)
    max_phase_visits: dict[str, int] = Field(default_factory=dict)


class App(BaseModel):
    name: str
    entry_phase: str
    phases: dict[str, Phase]
    graph: AppGraph
    final_output_schema: dict[str, Any]
    final_output_name: str
    final_output_description: str = ""
    # criteria the LLM must satisfy before the OS allows finish
    finish_criteria: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_final_output_name(self) -> "App":
        if not self.final_output_name.strip():
            raise ValueError(
                "App.final_output_name must not be empty. "
                "Set it to the artifact type name the LLM should use for the final output."
            )
        return self


class FileIROp(BaseModel):
    kind: Literal["file"]
    op: Literal["read", "write"]
    path: str
    content: str | None = None


class ToolIROp(BaseModel):
    kind: Literal["tool"]
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class MCPIROp(BaseModel):
    kind: Literal["mcp"]
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SubAgentIROp(BaseModel):
    kind: Literal["subagent"]
    agent: str
    input: dict[str, Any] = Field(default_factory=dict)


class AskUserIROp(BaseModel):
    kind: Literal["ask_user"]
    question: str
    suggestions: list[str] = Field(default_factory=list)
    required: bool = True


# Discriminated union — Pydantic selects the variant via the "kind" field.
# "file" and "ask_user" are implemented; others are safely skipped by ControlIRExecutor.
ControlIROp = Annotated[
    Union[FileIROp, ToolIROp, MCPIROp, SubAgentIROp, AskUserIROp],
    Field(discriminator="kind"),
]


class ControlReason(BaseModel):
    """Structured reason object — extensible for future fields."""
    summary: str


class ControlDecision(BaseModel):
    """Routing decision returned by the LLM. Strict contract — no runtime inference."""
    type: Literal["transition", "finish", "abort"]
    decision: Literal["continue", "finish", "abort"]
    next_phase: str | None = None  # phase name for transition; None for finish/abort
    confidence: float = 1.0
    reason: ControlReason

    @property
    def effective_next_phase(self) -> str:
        """Maps control decision to the candidate_map key ("end" for finish)."""
        if self.type == "finish":
            return "end"
        return self.next_phase or ""


class CandidateOutput(BaseModel):
    """A single candidate the LLM may choose for its next step."""
    next_phase: str                                        # phase name, or "end"
    control_type: Literal["transition", "finish"] = "transition"
    schema_name: str                                       # artifact type name
    artifact_schema: dict[str, Any]
    description: str = ""


class LLMOutput(BaseModel):
    """Unified LLM output — control decision + artifact."""
    control: ControlDecision
    artifact: dict[str, Any]
    control_ir: list[ControlIROp] = Field(default_factory=list)

    @property
    def next_phase(self) -> str:
        return self.control.effective_next_phase


class ControlIROpSpec(BaseModel):
    """Describes one kind of Control IR operation available to the LLM."""
    kind: str
    description: str
    example: dict[str, Any]  # minimal valid example for this kind


class ExecutionState(BaseModel):
    """Structured execution history injected into ContextFrame."""
    path: list[str] = Field(default_factory=list)  # "phase → next" transition strings, oldest first
    current_visit: int = 1   # how many times the current phase has been entered this run
    total_steps: int = 0     # total LLM calls completed across all phases so far


class PhaseConstraints(BaseModel):
    """Operational limits for the current phase, surfaced to the LLM."""
    max_phase_visits: int | None = None   # per-phase visit cap (None = unlimited)


class ContextFrame(BaseModel):
    current_phase: str
    current_phase_role: str | None = None
    instructions: str
    input_artifact: dict[str, Any]
    execution: ExecutionState = Field(default_factory=ExecutionState)
    candidate_outputs: list[CandidateOutput]
    finish_criteria: list[str] = Field(default_factory=list)
    constraints: PhaseConstraints = Field(default_factory=PhaseConstraints)
    available_control_ops: list[ControlIROpSpec] = Field(default_factory=list)
    output_language: str = "ja"
    # Populated when a previous control_ir op in this phase produced a result
    # (file read content, ask_user answer, etc.). Empty on first LLM call for the phase.
    # Each entry is the raw result dict returned by ControlIRExecutor.execute().
    control_ir_results: list[dict] = Field(default_factory=list)


class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = Field(default_factory=dict)
