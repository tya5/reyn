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
    permissions: dict = field(default_factory=dict)  # raw frontmatter permissions block
    preprocessor: list[dict] = field(default_factory=list)  # raw YAML config; typed in expander


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
