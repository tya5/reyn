from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_serializer, model_validator

from reyn.security.permissions.permissions import PermissionDecl


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
    char_offset: int | None = None   # #2335: char position WITHIN line `offset` to resume from (paging a single line longer than the inline cap); None = start of the line
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
    char_offset: int | None = None   # #2335: char position WITHIN line `offset` to resume from (paging a single over-cap line); None = start of the line


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


class MCPReadResourceIROp(BaseModel):
    """#2597 slice ②a: read one MCP resource (or a resolved resource-template URI)
    by URI. Permission-gated + event-emitting like ``MCPIROp`` (external, possibly
    sensitive server-authored content) — unlike the discovery-only
    ``mcp_list_resources``/``mcp_list_resource_templates`` path, which mirrors
    ``list_tools`` (no permission gate, no op-kind)."""
    kind: Literal["mcp_read_resource"]
    server: str
    uri: str


class MCPSubscribeResourceIROp(BaseModel):
    """#2597 slice ②b: subscribe to server-pushed ``resources/updated`` for one
    URI on a configured MCP server. Permission-gated like ``MCPReadResourceIROp``
    (same server-scoped ``require_mcp`` axis — subscribing is a stateful action
    against the server, gated the same way a read is). The push notification
    itself lands as an ``mcp_resource_updated`` EventLog event (see
    ``reyn.mcp.message_handler.ReynMCPMessageHandler.on_resource_updated``), not
    as this op's return value — this op only confirms the subscribe request
    succeeded."""
    kind: Literal["mcp_subscribe_resource"]
    server: str
    uri: str


class MCPUnsubscribeResourceIROp(BaseModel):
    """#2597 slice ②b: inverse of ``MCPSubscribeResourceIROp``."""
    kind: Literal["mcp_unsubscribe_resource"]
    server: str
    uri: str


class MCPGetPromptIROp(BaseModel):
    """#2597 slice ②c: fetch one rendered MCP prompt (messages) by name.
    Permission-gated + event-emitting like ``MCPReadResourceIROp`` (external,
    possibly sensitive server-authored content) — unlike the discovery-only
    ``mcp_list_prompts`` path, which mirrors ``list_tools``/``list_resources``
    (no permission gate, no op-kind)."""
    kind: Literal["mcp_get_prompt"]
    server: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AskUserIROp(BaseModel):
    kind: Literal["ask_user"]
    question: str
    suggestions: list[str] = Field(default_factory=list)
    # F3 (region framework): a closed set of selectable answers. Empty → free-text
    # (current behaviour). Non-empty → the user picks one (rendered as a selector
    # in the inline CUI region; typed by number on the stdin / --cui path).
    options: list[str] = Field(default_factory=list)
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
    stdin: bytes | None = None                           # #2593: bytes written to the process's stdin, if any






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
    a distinct decl field (``mcp_drop_server``) so an agent must
    explicitly declare drop intent — install intent alone is
    insufficient. This prevents an install-only agent from
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


class SkillInstallIROp(BaseModel):
    """Register a skill into the project skills config (#2548 PR-C/PR-D).

    Resolves the skill's ``SKILL.md`` frontmatter to extract the name and
    description, then writes a ``skills.entries.<name>`` entry to
    ``.reyn/config/skills.yaml``. Mirrors the ``mcp_install`` handler pipeline:
    threat-scan → permission gate → config write → record_config_generation →
    emit event → hot-reload.

    Two install paths:
    - **Local path** (``source is None``): ``path`` points at a local directory
      (resolved to ``<dir>/SKILL.md``) or a direct SKILL.md file.
    - **Source path** (``source`` set): ``source`` is a git URL or GitHub URL.
      The handler shallow-clones the repo to ``.reyn/skills/<name>/``, reads the
      SKILL.md from that clone, and registers the installed copy's path.
      ``path`` is ignored when ``source`` is set.

    ``name`` overrides the frontmatter name when set (useful when the directory
    basename differs from the desired config key). ``scope`` is retained for
    forward compat with multi-tier support; currently always resolves to
    ``.reyn/config/skills.yaml``.
    """
    kind: Literal["skill_install"]
    path: str = ""                          # local dir or direct SKILL.md path (ignored when source set)
    scope: str = ".reyn/config/skills.yaml"  # target config file (no-op tier arg)
    name: str | None = None                 # override the frontmatter / dir-basename name
    # When set, the skill is fetched from this git/GitHub URL (registry fetch skipped).
    # Supports optional subdir via "//": "https://github.com/user/repo" (root) or
    # "https://github.com/user/repo//skills/my-skill" (skills/my-skill subdir).
    source: str | None = None              # git/GitHub URL (installs to .reyn/skills/<name>/)


# ---------------------------------------------------------------------------
# RAG-extensible OS (ADR-0033) — embed / index_* / recall ops + ChunkMetadata
# ---------------------------------------------------------------------------
# Phase 1 of FP-0002 / ADR-0033. ChunkMetadata is the OS-level data carrier
# passed between embed / index_write / index_query / recall. The `source_type`
# value is NOT interpreted by OS code (= P7); chunker modules and agents
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
    extra: dict[str, Any] = Field(default_factory=dict)  # caller-defined fields


# #1303 Stage I: EmbedIROp + IndexWriteIROp deleted. The chunkers stream into
# reyn.api.safe.embed_index (provider.embed + SqliteIndexBackend directly) and
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

    P7 note: rubric content is owned by the phase/agent author; the OS is rubric-
    agnostic and never interprets it. `on_fail` uses OS-level vocabulary only.
    """
    kind: Literal["judge_output"]
    target: str              # JSONPath-like dot path, e.g. "artifact.data.summary"
    rubric: str              # LLM prompt body; phase/agent author owns content (P7)
    threshold: float = 0.8  # passing score in [0.0, 1.0]
    on_fail: Literal["transition", "abort", "continue"] = "transition"
    model: str | None = None  # model class override; None = inherit from ctx




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

    P7/P8 note: carries no domain-specific fields. ``reason`` is optional
    free-text the model may supply for the audit trail; the OS never
    interprets it.
    """
    kind: Literal["compact"]
    reason: str | None = None   # optional model-supplied rationale (audit only)


# ---------------------------------------------------------------------------
# Task ops (#1953 slice 1) — first-class trackable work-units.
# ---------------------------------------------------------------------------
# Agent-facing Task operations as Control IR ops (P4). P7 term-neutral: op
# names + fields are generic; A2A vocabulary (contextId / TaskState) maps only
# at the A2A layer. Single-writer is a backend-CAS on the caller's
# run_id (threaded from OpContext, NOT an op field → unforgeable; audit C2),
# NOT a permission gate. Enforcement (CAS reject, abort quiescence, cascade,
# cycle-check, predicate-eval) lands in later slices; these are the shapes.


class TaskCreateIROp(BaseModel):
    """Create a Task. The **requester** is the caller's session (set by the OS,
    not an op field — §model "requester=self"); the **assignee** is the worker
    session, immutable for the Task's life (no handoff — §12). ``assignee``
    defaults to the caller (a self-task); a different value delegates cross-session.

    Ownership is OS-derived (§16 recursive-request): a sub-task created while a
    session executes a task-as-request T is owned by T (``requester=T``,
    ``requester_kind=task``, set from the execution context — never an op field).
    The legacy ``parent_id`` tree was removed (§16 slice C); ``deps`` are
    depends-on edges (dependency DAG, §13)."""

    kind: Literal["task.create"]
    name: str
    assignee: str | None = None  # default: the caller's own session (self-task)
    description: str | None = None
    deps: list[str] = Field(default_factory=list)  # depends-on task_ids (DAG, §13)
    # #2187 §3.5 (5b): the decomposition-link type when this is a sub-task — `awaited`
    # gates the parent's completion (the parent blocks on the result), `background`
    # runs parallel (never blocks). Default awaited (the safe, blocking default);
    # consulted only when the OS derives requester_kind=task.
    link_type: Literal["awaited", "background"] = "awaited"


class TaskUpdateStatusIROp(BaseModel):
    """Declare a status transition. Writer = the task's **assignee session**; the
    gate is a fixed-equality backend CAS ``assignee == caller_session_id``
    (``OpContext.session_id``, the #1814 routing-key — threaded, not a field here).
    The assignee is immutable, so no claim token / run_id / version is needed; a
    terminal task rejects all writes (the cooperative-terminal guard)."""

    kind: Literal["task.update_status"]
    task_id: str
    # #2187 followup: the assignee-SETTABLE lifecycle transitions only (running = start,
    # done = complete, failed = declare failure). A `Literal` (not a bare str) so the OS
    # injects the valid values to the LLM (P8) and Control-IR validation REJECTS an
    # invalid/stale value — a weak model emitting the pre-#2187 "completed" (or any other
    # string) is caught at op-validation, never written. `aborted` is a separate op
    # (`task.abort`); `unassigned`/`blocked`/`ready` are OS-derived, never assignee-set.
    status: Literal["running", "done", "failed"]
    reason: str | None = None


class TaskGetIROp(BaseModel):
    """Read one Task record."""

    kind: Literal["task.get"]
    task_id: str


class TaskListIROp(BaseModel):
    """List Tasks, optionally narrowed by assignee / requester / status.

    Narrowing by ``requester`` (a task-as-request id) is the ownership query — it
    lists the sub-tasks that task owns (§16 recursive-request)."""

    kind: Literal["task.list"]
    assignee: str | None = None
    requester: str | None = None
    status: str | None = None


class TaskAddDependencyIROp(BaseModel):
    """Add a depends-on edge (dependency DAG, §13). Topology owned by the
    decomposing requester; readiness is derived read-only (no write to the dep).
    Existence + cycle-checked via the shared edge-guard (slice 6)."""

    kind: Literal["task.add_dependency"]
    task_id: str
    depends_on: str


class TaskRemoveDependencyIROp(BaseModel):
    """Drop a depends-on edge (#1953 slice 6-ext). Requester topology write,
    idempotent (no-op on a missing edge). Dropping an edge only relaxes the graph,
    so the OS re-derive may promote a now-satisfied blocked dependent (incl. the
    last-dep-removed → ready case); it never demotes."""

    kind: Literal["task.remove_dependency"]
    task_id: str
    depends_on: str


class TaskRepointDependencyIROp(BaseModel):
    """Atomically repoint an edge ``from_depends_on`` → ``to_depends_on`` (#1953
    slice 6-ext) — the parent's primary recovery move (point a dependent at a
    substitute). The NEW edge is cycle-checked BEFORE any mutation (a cycle/dangling
    repoint changes nothing, returning the structured error); then the dependent's
    readiness is re-blocked + re-evaluated against the new graph."""

    kind: Literal["task.repoint_dependency"]
    task_id: str
    from_depends_on: str
    to_depends_on: str


class TaskAbortIROp(BaseModel):
    """Requester remove-op (= delete) → ``aborted``/``archived`` (A2A canceled).
    Cooperative-terminal: it archives the task + its sub-tree (DOWN-cascade); there
    is no forced cancel — the assignee's in-flight work is rejected by the terminal
    guard at its next status-write (no straggler, no sibling-kill)."""

    kind: Literal["task.abort"]
    task_id: str
    reason: str | None = None


class TaskHeartbeatIROp(BaseModel):
    """Liveness + (slice 7) unblock-predicate evaluation trigger for a blocked
    task. Returns the current state; predicate-eval / liveness-timeout land in
    slice 7."""

    kind: Literal["task.heartbeat"]
    task_id: str


class TaskRegisterUnblockPredicateIROp(BaseModel):
    """Register a deterministic unblock predicate (code, no LLM) evaluated at
    heartbeat (§22 tier B'); true → unblock → LLM only then. Predicate-eval is
    slice 7; this records it."""

    kind: Literal["task.register_unblock_predicate"]
    task_id: str
    predicate: str


class TaskCommentIROp(BaseModel):
    """Append a comment to a Task's thread (durable inter-agent/HITL protocol —
    Hermes gap7). Core-contract field; richer thread semantics deferred."""

    kind: Literal["task.comment"]
    task_id: str
    body: str


class TaskAssignIROp(BaseModel):
    """Assign a session to a task (#2187 §27-31, the pending-assignment queue). An
    UNASSIGNED task may be claimed by anyone; an assigned task may be reassigned only
    by its current owner (the assignee) — the op layer gates this. Rebinds the
    WAL subscription (``record_rebound``) and re-derives the now-startable status."""

    kind: Literal["task.assign"]
    task_id: str
    assignee: str  # the (agent,session) routing-key to bind as the new executor


# ── Op-kind registry — the single source for the Control IR op surface ───────
# #1983: OP_KIND_MODEL_MAP is co-located HERE (relocated from
# op_runtime/registry.py) so the Op union, ALL_OP_KINDS, and
# op_runtime's purity / op_catalog all derive from ONE map. Previously the map
# lived in registry.py — which imports these model classes — so models.py could
# not derive the union from it without a cycle; that dual-source (hand-listed
# union vs the map) was #1983's root cause. Add a new op kind HERE (kind → IROp
# model); the union + ALL_OP_KINDS follow by construction. op_runtime/registry.py
# re-imports ALL_OP_KINDS from
# here (intentional convenience, not a migration shim).
#
# NOTE: the coarse "file" kind is intentionally NOT in the map (#1240 Wave 2b
# dropped it; fine handlers still build FileIROp(kind="file") internally). It is
# the one explicit non-map member of the union below.
OP_KIND_MODEL_MAP: dict[str, type[BaseModel]] = {
    "read_file":   ReadFileIROp,
    "write_file":  WriteFileIROp,
    "edit_file":   EditFileIROp,
    "delete_file": DeleteFileIROp,
    "glob_files":  GlobFilesIROp,
    "grep_files":  GrepFilesIROp,
    "mcp":         MCPIROp,
    # #2597 slice ②a: resources consumption — read is permission-gated (external
    # content); list/list-templates stay op-kind-free, mirroring list_tools (see
    # op_runtime/mcp_read_resource.py + session.py's _mcp_list_resources).
    "mcp_read_resource": MCPReadResourceIROp,
    # #2597 slice ②b: resource subscriptions — subscribe/unsubscribe are
    # permission-gated the same way read is (stateful action against the
    # server); the resulting push notification is an EventLog event
    # (mcp_resource_updated), not routed through the Op union at all.
    "mcp_subscribe_resource": MCPSubscribeResourceIROp,
    "mcp_unsubscribe_resource": MCPUnsubscribeResourceIROp,
    # #2597 slice ②c: prompts consumption — get is permission-gated (external
    # content); list stays op-kind-free, mirroring list_tools/list_resources (see
    # op_runtime/mcp_get_prompt.py + session.py's _mcp_list_prompts).
    "mcp_get_prompt": MCPGetPromptIROp,
    "ask_user":    AskUserIROp,
    "web_fetch":   WebFetchIROp,
    "web_search":  WebSearchIROp,
    "mcp_install": MCPInstallIROp,
    # #1983: was registered + documented (control-ir.md) + handled but ABSENT
    # here → a phase emitting it failed Op union validation. Added to restore
    # the control-ir.md ↔ map sync invariant.
    "mcp_drop_server": MCPDropServerIROp,
    "index_query": IndexQueryIROp,
    "recall":      RecallIROp,
    "index_drop":  IndexDropIROp,
    "sandboxed_exec": SandboxedExecIROp,
    "judge_output": JudgeOutputIROp,
    "compact": CompactIROp,
    "task.create": TaskCreateIROp,
    "task.update_status": TaskUpdateStatusIROp,
    "task.get": TaskGetIROp,
    "task.list": TaskListIROp,
    "task.add_dependency": TaskAddDependencyIROp,
    "task.remove_dependency": TaskRemoveDependencyIROp,
    "task.repoint_dependency": TaskRepointDependencyIROp,
    "task.abort": TaskAbortIROp,
    "task.heartbeat": TaskHeartbeatIROp,
    "task.register_unblock_predicate": TaskRegisterUnblockPredicateIROp,
    "task.comment": TaskCommentIROp,
    "task.assign": TaskAssignIROp,
    # #2548 PR-C: local skill directory install — register a SKILL.md dir into
    # skills.entries (parallel to mcp_install writing mcp.servers).
    "skill_install": SkillInstallIROp,
}

# Frozenset of op kinds — DSL linter, contextual gate.
ALL_OP_KINDS: frozenset[str] = frozenset(OP_KIND_MODEL_MAP.keys())

# Discriminated union — DERIVED from OP_KIND_MODEL_MAP (#1983, completeness-by-
# construction: any kind in the map is in the union → no dual-source). FileIROp
# (coarse "file") is the only explicit non-map member (internal-use, see note
# above). Pydantic accepts the dynamically-built discriminated Union (verified).
if TYPE_CHECKING:
    # Static mirror for type-checkers only — mypy can't evaluate the runtime-built
    # Union. The RUNTIME value derives from the map (below); this list is NOT the
    # source of truth and is pinned in sync by the completeness-invariant test
    # ({union kinds} == ALL_OP_KINDS ∪ {"file"}).
    Op = Annotated[
        Union[
            FileIROp,
            ReadFileIROp, WriteFileIROp, EditFileIROp, DeleteFileIROp,
            GlobFilesIROp, GrepFilesIROp,
            MCPIROp, MCPReadResourceIROp,
            MCPSubscribeResourceIROp, MCPUnsubscribeResourceIROp,
            MCPGetPromptIROp,
            AskUserIROp,
            WebFetchIROp, WebSearchIROp, MCPInstallIROp,
            MCPDropServerIROp,
            IndexQueryIROp, RecallIROp, IndexDropIROp,
            SandboxedExecIROp, JudgeOutputIROp, CompactIROp,
            TaskCreateIROp, TaskUpdateStatusIROp, TaskGetIROp, TaskListIROp,
            TaskAddDependencyIROp, TaskRemoveDependencyIROp, TaskRepointDependencyIROp,
            TaskAbortIROp,
            TaskHeartbeatIROp, TaskRegisterUnblockPredicateIROp,
            TaskCommentIROp,
            SkillInstallIROp,
        ],
        Field(discriminator="kind"),
    ]
else:
    Op = Annotated[
        Union[tuple([FileIROp, *OP_KIND_MODEL_MAP.values()])],
        Field(discriminator="kind"),
    ]



class Event(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    data: dict[str, Any] = Field(default_factory=dict)
