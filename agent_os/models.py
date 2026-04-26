from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


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
    final_output_name: str = ""
    final_output_description: str = ""
    # criteria the LLM must satisfy before the OS allows finish
    finish_criteria: list[str] = Field(default_factory=list)


class ControlIROp(BaseModel):
    op: Literal["write_file", "read_file"]
    path: str
    content: str | None = None


class ControlReason(BaseModel):
    """Structured reason object — extensible for future fields."""
    summary: str


class ControlDecision(BaseModel):
    """Routing decision returned by the LLM. Strict contract — no runtime inference."""
    type: Literal["transition", "finish", "abort"]
    decision: Literal["continue", "revise", "finish", "abort"]
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


class ContextFrame(BaseModel):
    current_phase: str
    current_phase_role: str | None = None
    instructions: str
    input_artifact: dict[str, Any]
    history_summary: str
    candidate_outputs: list[CandidateOutput]
    finish_criteria: list[str] = Field(default_factory=list)
    output_language: str = "ja"
    current_phase_visit: int = 1
    max_phase_visit: int | None = None


class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = Field(default_factory=dict)
