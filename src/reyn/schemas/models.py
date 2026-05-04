from __future__ import annotations
from datetime import datetime
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, Field, model_validator
from reyn.permissions.permissions import PermissionDecl


# ── Preprocessor step types ───────────────────────────────────────────────────

class ValidateStep(BaseModel):
    type: Literal["validate"]
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class IterateStep(BaseModel):
    type: Literal["iterate"]
    over: str                                   # dot path to an array in the input artifact
    apply: "PreprocessorStep"                   # nested step (run_op typically)
    into: str                                   # dot path where the collected array is placed
    on_error: Literal["fail", "skip"] = "fail"


class LintPlanStep(BaseModel):
    """
    Run deterministic structural checks (cycle, artifact coverage, etc.) on a
    plan-shaped dict embedded in the input artifact. Issues are appended at
    `into` for the LLM to act on. Does NOT abort on issues — enrichment only.
    """
    type: Literal["lint_plan"]
    over: str = "data"  # dot path to the plan dict; default: artifact["data"]
    into: str           # dot path where the list of issue strings is placed


class PythonStep(BaseModel):
    """Run a user-supplied Python function as a deterministic preprocessor step.

    Phase declares both the function (here) and the permission to call it
    (in `permissions.python`). The function executes in a subprocess via
    reyn._python_harness with the user's chosen mode (pure / trusted),
    timeout, and 3rd-party allowlist. Its return value is placed at
    `into` and validated against `output_schema` so the LLM sees a
    typed enriched artifact.
    """
    type: Literal["python"]
    module: str               # skill-dir-relative path, e.g. "./preprocessing.py"
    function: str             # function name within the module
    into: str                 # dot path in artifact where the return value is placed
    output_schema: dict[str, Any]  # JSON Schema of the function's return value


class RunOpStep(BaseModel):
    """Invoke any ControlIROp from the static (preprocessor) frontend.

    `op` is a literal ControlIROp embedded directly. `args_from` lets
    selected fields be replaced with values pulled from dot-paths in the
    input artifact at execution time (useful inside `iterate`, where
    each item's data needs to flow into the op).

    `ask_user` cannot be invoked here — the op_runtime dispatcher rejects
    it because static execution can't pause for user input.
    """
    type: Literal["run_op"]
    op: "ControlIROp"
    into: str | None = None
    args_from: dict[str, str] = Field(default_factory=dict)
    on_error: Literal["fail", "skip", "empty"] = "fail"

    @model_validator(mode="after")
    def _check_ask_user(self) -> "RunOpStep":
        if getattr(self.op, "kind", None) == "ask_user":
            raise ValueError(
                "run_op cannot wrap an ask_user op — preprocessor steps "
                "execute statically and cannot pause for user input."
            )
        return self


PreprocessorStep = Annotated[
    Union[RunOpStep, IterateStep, ValidateStep, LintPlanStep, PythonStep],
    Field(discriminator="type"),
]

# Postprocessor uses the same step set as preprocessor (`RunOpStep` /
# `IterateStep` / `ValidateStep` / `LintPlanStep` / `PythonStep`). The alias
# below keeps callsites readable when they're operating in postprocessor
# context, while the discriminated union itself is shared.
ProcessorStep = PreprocessorStep

# IterateStep / RunOpStep both use forward refs that resolve only after
# ControlIROp is defined further down. The rebuild is performed at the
# bottom of this file once all referenced types are in scope.


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
    preprocessor: list[PreprocessorStep] = Field(default_factory=list)
    # Control IR op kinds the phase may use. Filters available_control_ops in the
    # ContextFrame and is enforced at executor dispatch (defense in depth). The
    # default reflects the common case: file I/O plus user clarification. An
    # explicit empty list means "no ops" (e.g. pure routing phases).
    allowed_ops: list[str] = Field(
        default_factory=lambda: ["file", "ask_user"],
    )


class SkillNodeSpec(BaseModel):
    """Runtime descriptor for a skill node in a parent skill's graph."""
    skill_path: str              # absolute path to sub-skill's skill.md
    dsl_root: str                # dsl_root used to load the sub-skill
    workspace: str               # "isolated" | "shared"
    entry_input_schema: dict      # sub-app entry phase input_schema (for candidate building)
    entry_input_schema_name: str = "artifact"  # type name for display
    entry_input_description: str = ""


class SkillGraph(BaseModel):
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    can_finish_phases: list[str] = Field(default_factory=list)
    # "@skill_name" → SkillNodeSpec for app nodes embedded in this graph
    skill_nodes: dict[str, SkillNodeSpec] = Field(default_factory=dict)


class Postprocessor(BaseModel):
    """Skill-level postprocessor block — fires after the LLM finishes.

    Symmetric to the phase-level preprocessor (Phase.preprocessor), but lives
    at the **skill** boundary. The LLM is contracted against the skill's
    existing `final_output_schema`; postprocessor receives that artifact and
    transforms it into a (potentially richer) caller-facing artifact whose
    schema lives here.

    The step set, executable op set, on_error semantics, and permission gate
    are all identical to preprocessor — the only differences are fire
    position and the input/output schema source. See
    `docs/en/decisions/0017-...` family for the design rationale.
    """
    # Caller-facing output (what the skill returns to its invoker).
    output_schema: dict[str, Any]
    output_name: str = "artifact"
    output_description: str = ""
    steps: list[PreprocessorStep] = Field(default_factory=list)


class Skill(BaseModel):
    name: str
    description: str = ""
    doc: str = ""
    entry_phase: str
    phases: dict[str, Phase]
    graph: SkillGraph
    final_output_schema: dict[str, Any]
    final_output_name: str
    final_output_description: str = ""
    # criteria the LLM must satisfy before the OS allows finish
    finish_criteria: list[str] = Field(default_factory=list)
    # Skill-level permissions. The single source of truth for all permission
    # gating in this skill (startup_guard, postprocessor hooks, future
    # skill-wide steps). Populated by the expander directly from the skill.md
    # frontmatter `permissions:` block. Phase-level `permissions:` is
    # hard-rejected at parse time (ADR-0020).
    permissions: PermissionDecl = Field(default_factory=PermissionDecl)
    # Skill-level postprocessor. None = no postprocessor (the LLM's
    # `final_output_schema`-conformant artifact is returned as-is to the
    # caller). Non-None = postprocessor steps run after LLM finish, and the
    # caller receives an artifact conforming to `postprocessor.output_schema`.
    postprocessor: "Postprocessor | None" = None
    # Sub-apps referenced by preprocessor steps; pre-loaded at compile time.
    preprocessor_sub_skills: dict[str, "Skill"] = Field(default_factory=dict)
    # On-disk directory containing skill.md / phases/ / artifacts/. Populated by
    # the loader; used by python preprocessor steps to resolve relative module
    # paths. Empty string when the skill was constructed in memory.
    skill_dir: str = ""

    @model_validator(mode="after")
    def _require_final_output_name(self) -> "Skill":
        if not self.final_output_name.strip():
            raise ValueError(
                "Skill.final_output_name must not be empty. "
                "Set it to the artifact type name the LLM should use for the final output."
            )
        return self


# Skill rebuild is deferred to the bottom of this file (Skill.preprocessor
# references RunOpStep which forward-refs ControlIROp, defined further down).


class FileIROp(BaseModel):
    kind: Literal["file"]
    op: Literal["read", "write", "glob", "delete", "grep", "edit", "regenerate_index"]
    path: str                        # file path for read/write/edit/delete; glob pattern for glob; dir or file for grep; source dir for regenerate_index
    content: str | None = None       # write only
    max_results: int = 50            # glob only: cap on number of matching paths returned
    # read-specific
    offset: int | None = None        # line number to start reading from (0-indexed); None = beginning
    limit: int | None = None         # number of lines to read; None = all
    # grep-specific
    pattern: str | None = None       # regex pattern to search for
    glob: str | None = None          # file filter glob pattern (e.g. "**/*.py"); default searches all files
    file_type: str | None = None     # filter by file extension without dot (e.g. "py", "md")
    output_mode: Literal["content", "files_with_matches", "count"] = "content"
    case_insensitive: bool = False
    context_before: int = 0          # lines of context before each match
    context_after: int = 0           # lines of context after each match
    head_limit: int | None = None    # cap total number of matches returned
    # edit-specific
    old_string: str | None = None    # exact text to replace (must be unique unless replace_all=True)
    new_string: str | None = None    # replacement text
    replace_all: bool = False        # replace all occurrences instead of requiring uniqueness
    # regenerate_index-specific (PR19): build a markdown index from the
    # frontmatter of every `*.md` file under `path`. The OS layer is format-
    # agnostic — the caller provides `output_path`, `entry_template`, and
    # an optional `header`. Designed for memory MEMORY.md but reusable
    # for any "index from frontmatter" pattern.
    output_path: str | None = None   # absolute / cwd-relative path for the generated index file
    entry_template: str | None = None  # e.g. "- [{name}]({slug}.md) — {description}"; placeholders pulled from each body's frontmatter plus `slug` (filename without .md)
    header: str | None = None        # optional preamble prepended before the entries (e.g. "# Memory Index\n\n")


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
    skill_path: str            # workspace-relative path to the skill directory (e.g. "reyn/local/my_skill")


class RunSkillIROp(BaseModel):
    kind: Literal["run_skill"]
    skill: str                # skill name (resolved via search path) or path to skill.md
    input: dict               # input artifact to pass to the sub-skill
    model: str = ""           # model class or LiteLLM string; "" = inherit from runtime
    workspace: str = "isolated"  # "isolated" | "shared"
    # None = inherit caller's output_language (which may itself be None
    # = no language directive in LLM prompts; the LLM picks based on
    # user input). Reyn explicitly avoids regional-default fallbacks
    # (e.g. silently defaulting to "ja") because the project targets
    # a global audience.
    output_language: str | None = None


class WebFetchIROp(BaseModel):
    kind: Literal["web_fetch"]
    url: str                      # URL to fetch
    prompt: str = ""              # optional hint describing what to extract (informational for LLM)
    timeout: int = 30             # request timeout in seconds
    max_length: int = 50_000      # cap on returned content length (characters)


class WebSearchIROp(BaseModel):
    kind: Literal["web_search"]
    query: str                    # search query string
    max_results: int = 10         # cap on returned results
    backend: str = "duckduckgo"   # backend name (currently only "duckduckgo")


# Discriminated union — Pydantic selects the variant via the "kind" field.
# "file", "ask_user", "shell", "lint", "run_skill", "web_fetch", and "web_search" are implemented; others are safely skipped.
ControlIROp = Annotated[
    Union[FileIROp, ToolIROp, MCPIROp, SubAgentIROp, AskUserIROp, ShellIROp, LintIROp, RunSkillIROp, WebFetchIROp, WebSearchIROp],
    Field(discriminator="kind"),
]

# Resolve forward references now that ControlIROp is in scope.
RunOpStep.model_rebuild()
IterateStep.model_rebuild()
Skill.model_rebuild()


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
    control_type: Literal["transition", "finish", "rollback"] = "transition"
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
    # Field order is intentionally stable-first to maximize prompt-cache hit rate.
    # When serialized via model_dump(mode="json"), pydantic v2 preserves declaration
    # order. Fields that don't change across act-turns within a phase visit are
    # placed before fields that do, so the JSON prefix remains stable and caches.
    # Volatile fields (control_ir_results, current_datetime) live at the end.

    # ── stable across act-turns within a phase visit ───────────────────────────
    current_phase: str
    current_phase_role: str | None = None
    instructions: str
    candidate_outputs: list[CandidateOutput]
    finish_criteria: list[str] = Field(default_factory=list)
    constraints: PhaseConstraints = Field(default_factory=PhaseConstraints)
    available_control_ops: list[ControlIROpSpec] = Field(default_factory=list)
    # Reference catalog of every Control IR op kind the OS can dispatch in this
    # run, regardless of the current phase's allowed_ops. Populated for all
    # phases but only meta-skills (skill_builder, skill_improver, skill_importer)
    # consult it — they need to choose `allowed_ops` values for the phase
    # frontmatter they generate. Normal phases ignore this list.
    op_catalog: list[ControlIROpSpec] = Field(default_factory=list)
    # None = no explicit language directive in the LLM prompt; the LLM
    # picks the reply / artifact-text language based on user input
    # naturally. See `_system_prompt` in llm.py for how this is rendered.
    output_language: str | None = None
    model: str = ""        # model class name (or raw LiteLLM string) for this phase
    model_resolved: str = ""  # resolved LiteLLM string actually used for LLM calls
    input_artifact: dict[str, Any]
    execution: ExecutionState = Field(default_factory=ExecutionState)

    # ── volatile across act-turns ──────────────────────────────────────────────
    # Populated when a previous control_ir op in this phase produced a result
    # (file read content, ask_user answer, etc.). Empty on first LLM call for the phase.
    # Each entry is the raw result dict returned by ControlIRExecutor.execute().
    control_ir_results: list[dict] = Field(default_factory=list)
    # How many more act turns the LLM may emit before it MUST produce a decide turn.
    # 0 means this call is the mandatory decide turn — the LLM MUST NOT emit any ops.
    # None means unlimited (no max_act_turns constraint on this phase).
    remaining_act_turns: int | None = None
    current_datetime: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    data: dict[str, Any] = Field(default_factory=dict)
