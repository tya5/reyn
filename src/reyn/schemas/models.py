from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_serializer, model_validator

from reyn.security.permissions.permissions import PermissionDecl

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
    reyn._python_harness with the user's chosen mode (safe / unsafe),
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
    # #1240 Wave 2a: the default uses the fine-grained file kinds (the faithful
    # uniform equivalent of the legacy coarse `file` grant — same file
    # capability), so a phase that omits allowed_ops is born fine-native, keeping
    # the file→fine migration durable end-to-end (the legacy coarse `file` kind
    # is dropped in Wave 2b). No existing stdlib phase omits allowed_ops, so this
    # default change is behaviorally inert for them — it only shapes
    # future-generated / omitting skills.
    allowed_ops: list[str] = Field(
        default_factory=lambda: [
            "read_file", "write_file", "edit_file", "delete_file",
            "glob_files", "grep_files", "ask_user",
        ],
    )


class SkillNodeSpec(BaseModel):
    """Runtime descriptor for a skill node in a parent skill's graph."""
    skill_path: str              # absolute path to sub-skill's skill.md
    skill_root: str              # skill_root used to load the sub-skill
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
    `docs/deep-dives/decisions/0017-...` family for the design rationale.
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
    # Tool2Vec-style retrieval hints (FP-0024 Component B).
    # Optional list of example queries this skill can answer.  Absent in
    # existing skill.md files (backward-compat: None = not provided by author).
    # BM25/embedding backends concat these with the description to improve
    # Recall@5 pre-filter.  Integration with search backends is deferred to
    # the next wave (Track 3).
    search_hints: list[str] | None = None
    # FP-0016 Component D: per-skill credential scoping declaration.
    # Default ["*"] = full delegation (backward-compat — pre-FP-0016 behaviour
    # where sub-skills inherited all parent secrets). Authors opt into
    # scoping by listing specific keys ([], ["github_token"], etc.).
    # The OS reads this at run_skill boundaries to construct a
    # ScopedSecretStore for the sub-skill.
    required_credentials: list[str] = Field(default_factory=lambda: ["*"])

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
    op: Literal[
        "read", "write", "glob", "delete", "grep", "edit",
        "regenerate_index", "mkdir", "move", "stat",
    ]
    path: str                        # file path for read/write/edit/delete/stat; glob pattern for glob; dir or file for grep; source dir for regenerate_index; directory path for mkdir; source path for move
    content: str | None = None       # write only
    dest_path: str | None = None     # move only: destination path
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


# ── #1240 Wave 1: fine-grained file ops (phase = chat-tools subset) ──────────
# These fine kinds let phase Control IR emit the SAME tool names the chat
# catalog uses (read_file/write_file/edit_file/delete_file) instead of the
# coarse ``{kind:"file", op:<verb>}`` envelope — the catalog axis of the #1240
# 2-axis unification. Execution stays unified and there is NO backend
# duplication: the op_runtime fine handlers are thin adapters that build the
# coarse ``FileIROp`` and reuse the single ``file.handle()`` backend (same
# permission / WAL path). The coarse ``FileIROp`` is retained for
# behavior-preserving compat (Wave 2 migrates skills' ``allowed_ops`` to fine
# names + drops the coarse phase-only ToolDefinition).


class ReadFileIROp(BaseModel):
    kind: Literal["read_file"]
    path: str
    offset: int | None = None        # line number to start reading from (0-indexed); None = beginning
    limit: int | None = None         # number of lines to read; None = all


class WriteFileIROp(BaseModel):
    kind: Literal["write_file"]
    path: str
    content: str


class EditFileIROp(BaseModel):
    kind: Literal["edit_file"]
    path: str
    old_string: str                  # exact text to replace (must be unique unless replace_all=True)
    new_string: str                  # replacement text
    replace_all: bool = False        # replace all occurrences instead of requiring uniqueness


class DeleteFileIROp(BaseModel):
    kind: Literal["delete_file"]
    path: str


# ── #1240 Wave 1.5: glob_files / grep_files fine ops ─────────────────────────
# Same pattern as Wave 1's read_file/write_file/edit_file/delete_file above.
# Field names/types mirror the args the registry handler functions read:
#   _handle_glob (tools/file.py): path, pattern
#   _handle_grep (tools/file.py): path, pattern, glob, case_sensitive, max_results
# Both have phase=allow ToolDefinitions (GLOB_FILES/GREP_FILES, tools/file.py),
# so control_ir_executor routes them via the unified registry (same path as chat).


class GlobFilesIROp(BaseModel):
    kind: Literal["glob_files"]
    path: str = "."                  # root directory to search from
    pattern: str                     # glob pattern, e.g. "**/*.py"
    max_results: int = 50


class GrepFilesIROp(BaseModel):
    kind: Literal["grep_files"]
    path: str = "."                  # directory or file to search
    pattern: str                     # regex pattern to search for
    glob: str | None = None          # file filter glob, e.g. "**/*.py"
    case_sensitive: bool = False
    max_results: int = 50


class MCPIROp(BaseModel):
    kind: Literal["mcp"]
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class AskUserIROp(BaseModel):
    kind: Literal["ask_user"]
    question: str
    suggestions: list[str] = Field(default_factory=list)
    required: bool = True


class SandboxedExecIROp(BaseModel):
    """Execute a command under a SandboxPolicy (FP-0017).

    The replacement for the removed `shell` op (raw `subprocess.run`, #1352-A):
    this op routes through a SandboxBackend that enforces the declared policy. The
    OS selects the backend per platform; today the default is NoopBackend
    (= no enforcement). Future waves add SeatbeltBackend (macOS) and
    LandlockBackend (Linux).

    Policy fields mirror `reyn.security.sandbox.policy.SandboxPolicy` (= the dataclass
    the backend ultimately receives).
    """
    kind: Literal["sandboxed_exec"]
    argv: list[str]                                      # command + args; argv[0] is the executable
    network: bool = False                                # allow outbound network
    read_paths: list[str] = Field(default_factory=list)  # readable filesystem paths
    write_paths: list[str] = Field(default_factory=list) # writable filesystem paths
    allow_subprocess: bool = False                       # may spawn children
    env_passthrough: list[str] = Field(default_factory=list)  # env-var allowlist
    timeout_seconds: int = 60                            # wall-clock cap


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
    start_index: int = 0          # byte offset into extracted content for pagination (issue #357)


class WebSearchIROp(BaseModel):
    kind: Literal["web_search"]
    query: str                    # search query string
    max_results: int = 10         # cap on returned results
    backend: str = "duckduckgo"   # backend name (currently only "duckduckgo")


class MCPInstallIROp(BaseModel):
    kind: Literal["mcp_install"]
    server_id: str                             # registry identifier, e.g. "io.github.foo/bar-mcp"
    scope: Literal["local", "project", "user"] = "local"  # write-target config tier
    env_overrides: dict[str, str] | None = None  # pre-supplied env values (from --env flags)
    # When set, registry fetch is skipped and metadata is resolved from this
    # specifier directly (e.g. "npm:@modelcontextprotocol/server-filesystem",
    # "pypi:my-mcp-server", "docker:my-org/server", or a GitHub URL).
    # ``server_id`` is still required for the audit event; callers should
    # derive it from the specifier or accept an empty string for unnamed sources.
    source: str | None = None                  # --source specifier (skips registry fetch)
    # Extra args appended to the server's args list after registry/source resolution.
    # Useful for servers that require runtime flags (e.g. ["--server", "pyright"]).
    extra_args: list[str] | None = None


class MCPDropServerIROp(BaseModel):
    """Remove an MCP server from configuration (FP-0034 §D23).

    Counter-op to MCPInstallIROp:
      - install adds an entry to `mcp.servers.<short>`
      - drop_server removes that entry, optionally cleans secrets

    The op is purely mechanical (= LLM reasoning not needed) and lives
    in the universal catalog under ``mcp.operation__drop_server``. Per
    FP-0034 §D23, it parallels the existing ``reyn mcp remove`` CLI
    and emits a P6 ``mcp_server_removed`` event for audit.

    Permission gating mirrors mcp_install at the policy level but uses
    a distinct decl field (``mcp_drop_server``) so a skill must
    explicitly declare drop intent — install intent alone is
    insufficient. This prevents an install-only skill from
    accidentally tearing down user-configured servers.
    """
    kind: Literal["mcp_drop_server"]
    server: str                                # short config key (e.g. "filesystem")
    # When None, auto-detect by walking local → project → user scope
    # tiers and removing from the first that contains ``server``.
    scope: Literal["local", "project", "user"] | None = None
    # When True (default), also remove the corresponding `${KEY}=value`
    # entries from ~/.reyn/secrets.env keyed by the server's env block
    # at the time of removal. LLM-driven cleanup is more thorough than
    # the CLI default (which leaves secrets behind for safety; the LLM
    # has a clearer pruning intent).
    clear_secrets: bool = True


# ---------------------------------------------------------------------------
# RAG-extensible OS (ADR-0033) — embed / index_* / recall ops + ChunkMetadata
# ---------------------------------------------------------------------------
# Phase 1 of FP-0002 / ADR-0033. ChunkMetadata is the OS-level data carrier
# passed between embed / index_write / index_query / recall. The `source_type`
# value is NOT interpreted by OS code (= P7); chunker modules and skills
# attach domain-specific labels and read them back via filters.
# ---------------------------------------------------------------------------


class ChunkMetadata(BaseModel):
    """Per-chunk metadata carried between RAG ops (= ADR-0033 §2.1)."""
    source_path: str                       # generic — file path or memory slug
    source_type: str = "generic"           # OS does not interpret this value (P7)
    content_hash: str                      # change detection / dedup
    embedding_model: str                   # vector-space compatibility check
    chunk_index: int = 0                   # position within source
    size_tokens: int = 0                   # context budget management
    parent_context: str | None = None      # heading / class / function name
    extra: dict[str, Any] = Field(default_factory=dict)  # skill-defined fields


# #1303 Stage I: EmbedIROp + IndexWriteIROp deleted. The chunkers stream into
# reyn.safe.embed_index (provider.embed + SqliteIndexBackend directly) and
# recall embeds the query provider-direct, so neither run-op has any caller.
# EmbeddingProvider / SqliteIndexBackend / IndexQueryIROp / RecallIROp remain.


class IndexQueryIROp(BaseModel):
    """Semantic search over a single source (ADR-0033 §2.1).

    Inline-only input/output (top-K is small, ~30KB). Falls back to catalog
    enumeration when `query_vector` is None and the source is unindexed.
    """
    kind: Literal["index_query"]
    source: str                            # logical source name
    query_vector: list[float] | None = None  # None → enumerate fallback
    top_k: int = 5
    filters: dict[str, str] = Field(default_factory=dict)
    fallback_size_cap: int = 4096          # tokens, enumerate fallback cap


class RecallIROp(BaseModel):
    """Macro op: embed query → iterate index_query → merge top-K (ADR-0033 §2.1).

    Handler dispatches sub-ops via the OS dispatch path (= iterate op
    precedent). LLM-callable via ToolDefinition `recall`.
    """
    kind: Literal["recall"]
    query: str
    sources: list[str]                     # required, no default
    top_k: int = 5
    filters: dict[str, str] = Field(default_factory=dict)
    embedding_model: str = "standard"      # forwarded to embed sub-op


class IndexDropIROp(BaseModel):
    """Remove an indexed source entirely (ADR-0033 §2.1).

    `permissions.index_drop: ask` default (= ADR-0029 mirror, destructive op
    consent gate). LLM-callable via ToolDefinition `drop_source`.
    """
    kind: Literal["index_drop"]
    source: str


class JudgeOutputIROp(BaseModel):
    """LLM-based output scorer for in-phase evaluation loops (FP-0007 Component D).

    The OS resolves `target` to a value, calls an LLM with `rubric`, and
    returns a score (0.0–1.0) plus a pass/fail flag against `threshold`.

    P7 note: rubric content is owned by the skill author; the OS is rubric-
    agnostic and never interprets it. `on_fail` uses OS-level vocabulary only.
    """
    kind: Literal["judge_output"]
    target: str              # JSONPath-like dot path, e.g. "artifact.data.summary"
    rubric: str              # LLM prompt body; skill author owns content (P7)
    threshold: float = 0.8  # passing score in [0.0, 1.0]
    on_fail: Literal["transition", "abort", "continue"] = "transition"
    model: str | None = None  # model class override; None = inherit from ctx


class SkillResolveIROp(BaseModel):
    """Resolve a skill name to its on-disk skill.md path (R-PURE-MODE Wave 5a).

    Walks the canonical resolution chain (reyn/local/ → reyn/project/ →
    stdlib/) and returns path metadata. Read-only; no content is read.

    P7 note: `name` is the only skill-specific value; the OS does not
    interpret it beyond passing it to the resolution chain.
    """
    kind: Literal["skill_resolve"]
    name: str   # short skill name, e.g. "skill_improver" (no slashes or extensions)


class CompactIROp(BaseModel):
    """Voluntarily compact the conversation/phase history (#272 / #1128).

    LLM-emittable advisory control_ir op: when the OS-injected context-size
    signal shows the window is filling, the model can request a compaction
    rather than waiting for the involuntary ``retry_loop`` backstop. The OS
    runs the existing synchronous compaction (``force_compact_now``) and
    returns the freed tokens + the free window afterwards (exact tokens,
    unit-aligned with the load-contract / context-size signal) so the model
    can reason consistently about "should I compact" and "what fits now".

    Voluntary (LLM-initiated) and independent of the mandatory retry_loop
    backstop — this op never replaces it.

    P7/P8 note: carries no skill-specific fields. ``reason`` is optional
    free-text the model may supply for the audit trail; the OS never
    interprets it.
    """
    kind: Literal["compact"]
    reason: str | None = None   # optional model-supplied rationale (audit only)


# Discriminated union — Pydantic selects the variant via the "kind" field.
# All variants below are implemented in `op_runtime/`:
#   file, mcp, ask_user, shell, lint, run_skill, web_fetch, web_search,
#   mcp_install, embed, index_write, index_query, recall, index_drop,
#   sandboxed_exec, judge_output, skill_resolve, compact.
# Fine-grained file ops (#1240 Wave 1+1.5): read_file, write_file, edit_file,
#   delete_file, glob_files, grep_files (phase=allow registry entries).
ControlIROp = Annotated[
    Union[
        FileIROp,
        # #1240 Wave 1: fine-grained file ops (coarse FileIROp retained for compat).
        ReadFileIROp, WriteFileIROp, EditFileIROp, DeleteFileIROp,
        # #1240 Wave 1.5: glob_files / grep_files fine ops.
        GlobFilesIROp, GrepFilesIROp,
        MCPIROp, AskUserIROp, LintIROp,
        RunSkillIROp, WebFetchIROp, WebSearchIROp, MCPInstallIROp,
        IndexQueryIROp, RecallIROp, IndexDropIROp,
        SandboxedExecIROp, JudgeOutputIROp, SkillResolveIROp, CompactIROp,
    ],
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
    control_type: Literal["transition", "finish", "rollback", "abort"] = "transition"
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
    # #1212 reasoning-continuity: the model's own inline content emitted on prior
    # op-loop act turns (alongside tool_calls), carried forward so a capable model
    # keeps its reasoning thread across turns. Empty for weak models that emit no
    # inline content (e.g. flash-lite) and for the json-mode act loop. Bounded to
    # the last `recent_act_turns_raw` entries by the op-loop (no compaction LLM call).
    act_turn_reasoning: list[str] = Field(default_factory=list)
    # How many more act turns the LLM may emit before it MUST produce a decide turn.
    # 0 means this call is the mandatory decide turn — the LLM MUST NOT emit any ops.
    # None means unlimited (no max_act_turns constraint on this phase).
    remaining_act_turns: int | None = None
    # #1176 B1: OS-injected context-size signal (exact-token free-window header),
    # symmetric with the chat axis. None when the phase window is ample (the OS
    # omits it). Most per-turn-volatile → kept at the tail with the other
    # volatile fields so the cached frame prefix above it stays stable.
    context_size_signal: str | None = None
    current_datetime: datetime = Field(default_factory=lambda: datetime.now().astimezone())

    @model_serializer(mode="wrap")
    def _serialize(self, handler):  # noqa: ANN001
        """#1176 B1: omit ``context_size_signal`` entirely when absent (ample
        window) so a frame without a signal serializes byte-identically to the
        pre-#1176 shape — keeps the LLM-facing JSON and LLMReplay fixture keys
        stable. When the window is filling the signal rides in the volatile tail.
        #1212: same treatment for ``act_turn_reasoning`` — omit when empty so the
        op-loop (and json-mode) frames stay byte-identical to the pre-#1212 shape
        for every turn that carries no carried reasoning (json-mode, first turn,
        and weak models like flash-lite that emit no inline content). The field
        only rides the JSON when a capable model actually produced reasoning.
        All other fields are untouched (default serialization)."""
        data = handler(self)
        if data.get("context_size_signal") is None:
            data.pop("context_size_signal", None)
        if not data.get("act_turn_reasoning"):
            data.pop("act_turn_reasoning", None)
        return data


class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    data: dict[str, Any] = Field(default_factory=dict)
