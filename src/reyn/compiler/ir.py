from dataclasses import dataclass, field


@dataclass
class ArtifactDef:
    name: str
    schema: dict          # JSON Schema for the data object (no {type,data} wrapper)
    description: str = ""
    wrapped: bool = True  # False for entry-phase inputs (unwrapped flat schema)


@dataclass
class PhaseDef:
    name: str
    inputs: list[str]           # artifact names
    role: str | None
    can_finish: bool
    instructions: str
    max_act_turns: int = 0      # 0 = use system default (10)
    model_class: str = ""       # "light" | "standard" | "strong" | custom | "" = inherit from runtime
    preprocessor: list[dict] = field(default_factory=list)  # raw YAML config; typed in expander
    # Sentinel None = frontmatter omitted the key → expander applies the default.
    # Empty list = explicit "no ops".
    allowed_ops: list[str] | None = None


@dataclass
class SkillNodeDef:
    """An app referenced as a node in another app's graph."""
    skill_name: str    # e.g. "writing_review_app"
    workspace: str   # "isolated" | "shared"


@dataclass
class SkillDef:
    name: str
    description: str
    doc: str                             # body — human/LLM-readable usage guide
    entry: str
    edges: list[tuple[str, str]]        # (from_node, to_node) — nodes may be phases or @skill_names
    skill_nodes: dict[str, SkillNodeDef]    # "@skill_name" → SkillNodeDef
    final_output: str                    # artifact name for final_output_schema
    final_output_description: str
    finish_criteria: list[str]
    # Postprocessor block — raw frontmatter shape. Empty dict (the default)
    # = skill has no postprocessor. Expander typechecks and converts to
    # `schemas.models.Postprocessor`.
    postprocessor: dict = field(default_factory=dict)
    # Explicit skill-level permissions block — raw frontmatter shape.
    # Non-empty → expander uses this directly (bypasses phase union).
    # Empty / absent → expander falls back to _union_phase_permissions.
    permissions: dict = field(default_factory=dict)
    # Tool2Vec-style retrieval hints (FP-0024 Component B).
    # Optional list of example queries this skill can answer.  Absent in
    # existing skill.md files (backward-compat); BM25/embedding backends
    # concat these with the description to improve Recall@5.
    search_hints: list[str] = field(default_factory=list)
