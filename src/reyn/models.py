from __future__ import annotations
from datetime import datetime
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, Field, model_validator
from .permissions import PermissionDecl


# ── Preprocessor step types ───────────────────────────────────────────────────

class RunAppStep(BaseModel):
    type: Literal["run_app"]
    app: str            # sub-app name resolved at compile time
    into: str | None = None  # dot path where sub-app's final_output is placed;
                             # required at top level, None when nested inside iterate.apply


class ValidateStep(BaseModel):
    type: Literal["validate"]
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class IterateStep(BaseModel):
    type: Literal["iterate"]
    over: str                                   # dot path to an array in the input artifact
    apply: "PreprocessorStep"                   # nested step; MVP: RunAppStep only
    into: str                                   # dot path where the collected array is placed
    on_error: Literal["fail", "skip"] = "fail"


PreprocessorStep = Annotated[
    Union[RunAppStep, IterateStep, ValidateStep],
    Field(discriminator="type"),
]

# Resolve forward reference in IterateStep.apply
IterateStep.model_rebuild()


# ── Phase ─────────────────────────────────────────────────────────────────────

class Phase(BaseModel):
    name: str
    role: str | None = None
    input_schema: dict[str, Any]
    input_schema_name: str = "artifact"  # artifact type name(s) for display (e.g. "user_input")
    input_description: str = ""
    instructions: str
    max_act_turns: int = 10  # per-phase override; 0 = use system default
    model_class: str = ""   # "light"|"standard"|"strong"|custom; "" = inherit from runtime
    permissions: PermissionDecl = Field(default_factory=PermissionDecl)
    preprocessor: list[PreprocessorStep] = Field(default_factory=list)
    # Inferred by the compiler after the preprocessor chain is resolved.
    # Represents the artifact schema the LLM actually receives (input_schema + preprocessor enrichments).
    # None means no preprocessor → LLM sees input_schema unchanged.
    # Currently not used at runtime. Candidate future uses:
    #   1. Validate enriched artifact against this schema before the LLM call (preprocessor bug detection).
    #   2. Inject into ContextFrame so the LLM sees its actual input contract.
    llm_input_schema: dict[str, Any] | None = None


class AppNodeSpec(BaseModel):
    """Runtime descriptor for an app node in a parent app's graph."""
    app_path: str                # absolute path to sub-app's app.md
    dsl_root: str                # dsl_root used to load the sub-app
    workspace: str               # "isolated" | "shared"
    entry_input_schema: dict      # sub-app entry phase input_schema (for candidate building)
    entry_input_schema_name: str = "artifact"  # type name for display
    entry_input_description: str = ""


class AppGraph(BaseModel):
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    can_finish_phases: list[str] = Field(default_factory=list)
    # "@app_name" → AppNodeSpec for app nodes embedded in this graph
    app_nodes: dict[str, AppNodeSpec] = Field(default_factory=dict)


class App(BaseModel):
    name: str
    description: str = ""
    doc: str = ""
    entry_phase: str
    phases: dict[str, Phase]
    graph: AppGraph
    final_output_schema: dict[str, Any]
    final_output_name: str
    final_output_description: str = ""
    # criteria the LLM must satisfy before the OS allows finish
    finish_criteria: list[str] = Field(default_factory=list)
    # Sub-apps referenced by preprocessor steps; pre-loaded at compile time.
    preprocessor_sub_apps: dict[str, "App"] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_final_output_name(self) -> "App":
        if not self.final_output_name.strip():
            raise ValueError(
                "App.final_output_name must not be empty. "
                "Set it to the artifact type name the LLM should use for the final output."
            )
        return self


App.model_rebuild()


class FileIROp(BaseModel):
    kind: Literal["file"]
    op: Literal["read", "write", "glob", "delete"]
    path: str                        # file path for read/write; glob pattern for glob
    content: str | None = None       # write only
    max_results: int = 50            # glob only: cap on number of matching paths returned


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


class ShellIROp(BaseModel):
    kind: Literal["shell"]
    cmd: str                  # shell command to execute
    timeout: int = 120        # seconds


class LintIROp(BaseModel):
    kind: Literal["lint"]
    app_path: str              # workspace-relative path to the app directory (e.g. "reyn/local/my_app")


class RunAppIROp(BaseModel):
    kind: Literal["run_app"]
    app: str                  # app name (resolved via search path) or path to app.md
    input: dict               # input artifact to pass to the sub-app
    model: str = ""           # model class or LiteLLM string; "" = inherit from runtime
    workspace: str = "isolated"  # "isolated" | "shared"
    output_language: str = "ja"


# Discriminated union — Pydantic selects the variant via the "kind" field.
# "file", "ask_user", "shell", "lint", and "run_app" are implemented; others are safely skipped.
ControlIROp = Annotated[
    Union[FileIROp, ToolIROp, MCPIROp, SubAgentIROp, AskUserIROp, ShellIROp, LintIROp, RunAppIROp],
    Field(discriminator="kind"),
]


class ControlReason(BaseModel):
    """Structured reason object — extensible for future fields."""
    summary: str


class ControlDecision(BaseModel):
    """Routing decision returned by the LLM. Strict contract — no runtime inference."""
    type: Literal["transition", "finish", "abort", "rollback"]
    decision: Literal["continue", "finish", "abort"]
    next_phase: str | None = None  # phase name for transition; None for finish/abort/rollback
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


class ActOutput(BaseModel):
    """Act-turn output: execute ops and be re-called with results."""
    type: Literal["act"]
    ops: list[ControlIROp] = Field(default_factory=list)


class LLMOutput(BaseModel):
    """Decide-turn output: routing decision + artifact (+ optional write ops)."""
    control: ControlDecision
    artifact: dict[str, Any]
    ops: list[ControlIROp] = Field(default_factory=list)

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
    max_phase_visits: int | None = None   # global visit cap per phase (None = unlimited)


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
    model: str = ""        # model class name (or raw LiteLLM string) for this phase
    model_resolved: str = ""  # resolved LiteLLM string actually used for LLM calls
    # Populated when a previous control_ir op in this phase produced a result
    # (file read content, ask_user answer, etc.). Empty on first LLM call for the phase.
    # Each entry is the raw result dict returned by ControlIRExecutor.execute().
    control_ir_results: list[dict] = Field(default_factory=list)


class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = Field(default_factory=dict)
