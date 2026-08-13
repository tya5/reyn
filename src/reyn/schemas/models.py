from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


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
    absolute: bool = False           # glob only: return absolute paths even for a relative pattern (#3102)
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


class PresentIROp(BaseModel):
    """present op — route bulk data + a declarative view to the user-facing
    surface without the data round-tripping through LLM output tokens (FP-0054;
    ``view`` naming + optional view/blueprint: FP-0055 PR-1).

    **Tier 0** (``ask_user``'s sibling): presenting to the user — the trust root —
    is not an exfiltration channel, so there is no output permission gate. The one
    gate is that ``data_ref`` read authority resolves **identically to
    ``file.read``**: present can never read more than the agent's file ops can.
    Unlike ``ask_user``, present is **fire-and-continue** — it does NOT pause the
    run.

    Source (exactly one): ``data_ref`` — any zone-readable path; an offloaded
    ``structured_ref`` is re-hydrated to its full value via ``file.read``
    semantics — XOR ``data_inline`` — small data already in the LLM's context.

    View (at most one): ``view`` — a registered presentation name (resolved
    against the presentations registry) — XOR ``blueprint`` — an inline
    declarative component tree with JSON-Pointer (RFC 6901) path bindings,
    structurally gated to the display-only catalog (catalog components only,
    bindings are path expressions only). No markup / HTML / code ever crosses from
    the LLM to the renderer. **Both omitted is valid**: it means "no explicit
    view" and routes straight to the stage-3/4 default-viewer synthesis — a
    one-shot ``present(data_ref=...)`` "just shows" the data.
    """
    kind: Literal["present"]
    data_ref: str | None = None            # XOR data_inline; any zone-readable path
    data_inline: Any | None = None         # XOR data_ref; small already-in-context data
    view: str | None = None                # at most one of view/blueprint; a registered presentation name
    blueprint: dict[str, Any] | list[Any] | None = None  # at most one of view/blueprint; inline component tree

    @model_validator(mode="after")
    def _exactly_one_source_at_most_one_view(self) -> "PresentIROp":
        # data_inline may legitimately be a falsy value ({} / [] / 0); the
        # ``is None`` checks distinguish "absent" from "present-but-falsy".
        if (self.data_ref is None) == (self.data_inline is None):
            raise ValueError(
                "present requires exactly one of data_ref / data_inline"
            )
        if self.view is not None and self.blueprint is not None:
            raise ValueError(
                "present accepts at most one of view / blueprint (both omitted "
                "is valid — routes to the default viewer)"
            )
        return self


class RenderTemplateIROp(BaseModel):
    """render_template op — render a Jinja2 template against structured data into a
    plain string (FP-0055 PR-2).

    A general, sandboxed **producer**: ``data + template → string``. It has NO side
    effects and invokes no sink — the rendered string is returned as an ordinary op
    result whose bulk auto-offloads on the chat path; the caller routes it to any
    sink (``present``, a ``write_file``, a message, or a pipeline ``ctx``).
    Neutralization of the raw output is the SINK's job, never the producer's
    (producer-neutrality: a file is inert bytes, a terminal strips control bytes at
    its guard — different sinks disagree about what is dangerous).

    Template source (exactly one): ``template`` — an inline Jinja2 source string —
    XOR ``template_ref`` — a file path read as raw text under ``file.read``
    authority (a template file is source text, never JSON-rehydrated).

    Data source (exactly one): ``data_ref`` — any zone-readable path, re-hydrated to
    its full value under ``file.read`` semantics (the same seam ``present`` uses) —
    XOR ``data_inline`` — a small object already in the LLM's context. The resolved
    value binds under ``data`` in the template context (``{{ data.results[0] }}``).

    ``undefined``: ``strict`` (default) → an undefined variable is a HARD error
    naming the missing name (loud-by-default so a file sink never silently writes a
    broken artifact); ``lenient`` → undefined renders empty and the referenced-but-
    unbound names are reported in the result meta (``undefined_vars``).

    **Read-authority equivalence**: ``template_ref`` / ``data_ref`` resolve through
    exactly the ``file.read`` gate (a denied read → ``status="denied"``);
    render_template can never read more than the agent's ``file.read`` can. An
    inline-only invocation is pure computation (no read gate). The engine is always
    ``jinja2.sandbox.SandboxedEnvironment`` (SSTI-safe; templates may be
    LLM-authored) with autoescape OFF (HTML-escaping is a sink concern).
    """
    kind: Literal["render_template"]
    template: str | None = None            # XOR template_ref; inline Jinja2 source
    template_ref: str | None = None        # XOR template; a zone-readable template file path
    data_ref: str | None = None            # XOR data_inline; any zone-readable path (re-hydrated)
    data_inline: Any | None = None         # XOR data_ref; small already-in-context data
    undefined: Literal["strict", "lenient"] = "strict"

    @model_validator(mode="after")
    def _exactly_one_template_and_one_data(self) -> "RenderTemplateIROp":
        # data_inline may legitimately be a falsy value ({} / [] / 0 / ""); the
        # ``is None`` checks distinguish "absent" from "present-but-falsy".
        if (self.template is None) == (self.template_ref is None):
            raise ValueError(
                "render_template requires exactly one of template / template_ref"
            )
        if (self.data_ref is None) == (self.data_inline is None):
            raise ValueError(
                "render_template requires exactly one of data_ref / data_inline"
            )
        return self


class SandboxedExecIROp(BaseModel):
    """Execute a command under a SandboxPolicy (FP-0017).

    The replacement for the removed `shell` op (raw `subprocess.run`, #1352-A):
    this op routes through a SandboxBackend that enforces the declared policy. The
    OS selects the backend per platform; today the default is NoopBackend
    (= no enforcement). Future waves add SeatbeltBackend (macOS) and
    LandlockBackend (Linux).

    #3907: this op used to carry 5 sandbox-policy fields (`network` /
    `read_paths` / `write_paths` / `allow_subprocess` / `env_passthrough`) —
    removed. #1339 made the agent-level (operator) `sandbox.policy` win
    deterministically over anything the op requests, and #3907① measured that
    every context-building path resolves a concrete policy (never `None`), so
    those fields had zero real producers left: the one production
    construction site (`tools/exec.py`) never set them, and every remaining
    setter was a test constructing the op directly to exercise the (now
    dead) op-fields fallback in the handler. An unused field on an internal
    dataclass merely confuses a reader; an unused field on an op envelope is
    worse — it is advertised to and settable by the LLM, and silently does
    nothing (`exec.py`'s own tool description: "the LLM cannot set network /
    fs scope via this tool"). See `docs/reference/runtime/control-ir.md`'s
    `sandboxed_exec` section (kept in sync in this same PR, per CLAUDE.md's
    doc-drift hard rule) for the operator-facing story.

    #3962: `timeout_seconds` — the one field #3907's own sweep left behind —
    was the SAME defect class, just missed (it wasn't one of the 5 policy
    fields #3907 scoped to, since a wall-clock cap isn't a permission axis).
    #3907② deleted the op-fields fallback branch this field used to feed;
    once that branch was gone the value that actually governs a run's
    timeout was already only `ctx.default_sandbox_policy`'s own
    `timeout_seconds` (`SandboxPolicy`) — the op's copy had zero real
    readers, LLM-advertised and silently ignored, the exact gap #3907
    closed for the other 5. Removed rather than left as a second
    advertised-but-ignored knob.

    #3903① (2026-08-11): `timeout_seconds` is BACK — a deliberate, narrow
    reversal of #3962, not a repeat of the gap it closed. Owner ruling: the
    LLM should be able to extend the foreground wall-clock timeout past its
    default (`SandboxPolicy.timeout_seconds`), up to the OPERATOR's own
    configured ceiling (`SandboxPolicy.max_timeout_seconds` — never a
    hardcoded value; architect's conditional approval of this reversal
    required the ceiling stay operator-settable, since `timeout_seconds`
    was already an operator-config key, so a hardcoded LLM ceiling would
    let the LLM widen an operator's own narrower configuration, a Security
    pass-line violation). Unlike the removed field, THIS one is actually
    read (`op_runtime/sandboxed_exec.py`'s handler): `None` (default) uses
    the policy's own `timeout_seconds`; a value above
    `policy.max_timeout_seconds` is REJECTED (typed error naming the
    actual configured max), never silently clamped — a silent clamp would
    recreate #3962's advertised-but-ignored shape in a new form (the LLM
    would believe it got what it asked for).
    """
    kind: Literal["sandboxed_exec"]
    argv: list[str]                                      # command + args; argv[0] is the executable
    stdin: bytes | None = None                           # #2593: bytes written to the process's stdin, if any
    timeout_seconds: float | None = None                 # #3903①: optional LLM override, checked against SandboxPolicy.max_timeout_seconds






class WebFetchIROp(BaseModel):
    kind: Literal["web_fetch"]
    url: str                      # URL to fetch
    prompt: str = ""              # optional hint describing what to extract (informational for LLM)
    timeout: int = 30             # request timeout in seconds
    # #3580 ③: web_fetch has NO size cap of its own. The extracted text is returned
    # whole; the only ceiling on what reaches the model's context is the OS-level
    # tool-result cap (``offload.enabled``, default false = uncapped). The removed
    # ``max_length``/``start_index`` pair was a per-tool truncate + "paging" scheme
    # whose ``start_index`` never reached the tool schema, so the LLM could not page.


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
    # ADR 0064 §3.7 (plugin model P2): when set, stamped verbatim as
    # `entry["plugin_id"]` on the written skills.yaml entry — the additive
    # provenance field a `plugin_uninstall` reads back to find every registry
    # entry a given plugin created, across all three capability registries.
    # Not security-sensitive (unlike `provenance`, which is OS-stamped from
    # `ctx.turn_origin` alone to prevent an LLM self-declaring a gate bypass) —
    # this is bookkeeping only, so it is safe to accept as a caller-supplied
    # op field. None (the default) preserves pre-plugin-model behavior: a
    # direct skill_install never carries a plugin_id.
    plugin_id: str | None = None


class LoadSkillIROp(BaseModel):
    """Load a skill's ``SKILL.md`` body — the dedicated skill-activation verb
    (FP-0066 proposal §6/§11 P0, #3247; corrects the #2971 "no dedicated
    verb, reading IS the invocation" drift from ADR 0064 §3.5 / #3070's
    original call for one).

    Owns the FULL skill-load responsibility extracted out of ``file.read``'s
    former ``is_skill_body_path`` special-case: builtin/registered-plugin/
    config-registered PROVENANCE classification, permission-gated read,
    ``${REYN_*}``/``${CLAUDE_*}``/``${env:VAR}`` invocation-time expansion
    (``reyn.plugins.skill_load.load_skill_body``), and the **#3196
    symlink-swap-safe resolve-once** (the path is resolved EXACTLY ONCE per
    call and that single resolved string is reused for the permission gate,
    the provenance check, AND the actual byte read — never re-derived
    separately for "is this trusted" vs "what do I read", which is exactly
    the split #3196 closed).

    ``path`` is the skill's ``SKILL.md`` path as surfaced by ``skill_list`` /
    the L1 Skills menu — a registered entry's path, config-registered /
    builtin / plugin-shipped. A path that resolves to none of those
    provenance classes is still read (never a hard error — mirrors the
    pre-extraction fallback), but WITHOUT expansion and without a
    ``skill_body_loaded`` audit-event (fails closed, never open).

    LLM-callable via ToolDefinition ``load_skill`` (qualified name
    ``load_skill`` per the #3223 naming-convention arc —
    ratified, not provisional).
    """
    kind: Literal["load_skill"]
    path: str


class PipelineInstallIROp(BaseModel):
    """Register a pipeline into the project pipelines config (mirrors SkillInstallIROp).

    Writes a ``pipelines.entries.<name>`` entry to ``.reyn/config/pipelines.yaml``.
    Same handler pipeline as ``skill_install``: (threat-scan description) →
    permission gate → config write → record_config_generation → emit event →
    hot-reload.

    Two install paths:
    - **Local path** (``source is None``): ``path`` points at a pipeline DSL
      ``*.yaml`` file directly (unlike skills, there is no directory-or-file
      resolution — a pipeline registration is always exactly one file).
    - **Source path** (``source`` set): ``source`` is a git URL or GitHub URL.
      The handler shallow-clones the repo to ``.reyn/pipelines/<name>/``, reads
      the DSL file from that clone (``path`` selects the file inside the clone;
      required when the repo root/subdir contains more than one candidate),
      and registers the installed copy's path.

    ``name`` resolution (#2722): ``name`` is a free NAMESPACE KEY (like skill),
    NOT coupled to any declared ``pipeline:`` name. Every ``pipeline:`` document
    in the file registers under ``{name}.{declared-name}``. ``.`` is reserved
    (the namespace separator) and rejected in ``name``. When omitted, the key
    defaults to the DSL file stem (or the source basename for a git install).
    """
    kind: Literal["pipeline_install"]
    path: str = ""                              # local DSL *.yaml file (ignored when source set; also selects the file inside a cloned source repo)
    scope: str = ".reyn/config/pipelines.yaml"   # target config file (no-op tier arg, forward-compat with mcp_install's `scope`)
    name: str | None = None                      # free namespace key (#2722); no '.'; defaults to the DSL file stem
    # When set, the pipeline DSL is fetched from this git/GitHub URL (mirrors SkillInstallIROp.source).
    # Supports optional subdir via "//": "https://github.com/user/repo" (root) or
    # "https://github.com/user/repo//pipelines/my-pipeline" (pipelines/my-pipeline subdir).
    source: str | None = None                   # git/GitHub URL (installs to .reyn/pipelines/<name>/)
    # ADR 0064 §3.7 (plugin model P2): mirrors SkillInstallIROp.plugin_id
    # verbatim — see that field's docstring for the full rationale.
    plugin_id: str | None = None


class PresentationInstallIROp(BaseModel):
    """Register a named presentation template into the project presentations
    config (proposal 0060 Phase 1 Layer A, A8). Mirrors ``SkillInstallIROp`` /
    ``PipelineInstallIROp``'s STRUCTURE (permission gate → config write →
    ``record_config_generation`` → emit event → hot-reload), but the threat is
    LOWER than either — a present ``blueprint`` is structurally non-executable
    by construction (``reyn.core.present.catalog``: 8 fixed components, every
    non-literal value is a ``$bind`` RFC-6901 JSON-Pointer, no template-ref /
    eval / exec surface, ``image.src`` renders as a label — no fetch/SSRF).
    ``validate_blueprint`` (the SAME structural gate an inline ``present``
    blueprint already passes through) already fills the role
    ``scan_for_threats`` fills for skill/pipeline free-text ``description`` —
    so this op has no free-text field and therefore no ``scan_for_threats``
    call in its handler.

    Unlike skill/pipeline, there is no source/git-fetch path — a blueprint is
    small declarative data carried inline (mirrors
    ``reyn.data.presentations.registry``'s inline-blueprint-in-entry model),
    never a file-backed artifact.

    ``name`` is the registry key a ``present(view=...)`` op resolves against
    (mirrors the config-entries key). ``blueprint`` is the same declarative
    component tree an inline ``present(blueprint=...)`` accepts — it is
    validated through the identical :func:`validate_blueprint` gate before the
    entry is written, so a malformed / non-catalog blueprint is refused BEFORE
    any config mutation.
    """
    kind: Literal["presentation_install"]
    name: str                                    # registry key (present(view=<name>) resolves against this)
    blueprint: "dict[str, Any] | list[Any]"       # the declarative component tree (same shape as inline present)


# ---------------------------------------------------------------------------
# Plugin model (ADR 0064) — P2 install machinery.
# ---------------------------------------------------------------------------
# A plugin is a self-contained directory (manifest + optional mcp/pipelines/
# skills subdirs, ADR §3.1) promoted into `~/.reyn/plugins/<name>/` and
# registered against the SAME three capability registries `skill_install` /
# `pipeline_install` / (a local mcp entry) already write — `plugin_install`
# is an orchestration layer over those existing verbs (§3.2), not a fourth
# registry. `source` is a typed discriminated union (§3.8) — never a
# form-sniffed string the handler parses by shape (Tool Contract lens).


class PluginSourceBuiltin(BaseModel):
    """reyn's own shipped plugin under `src/reyn/builtin/plugins/<name>/`
    (ADR §3.1/§3.8). Lowest RCE trust risk — wins any name collision."""
    kind: Literal["builtin"] = "builtin"
    name: str


class PluginSourceLocal(BaseModel):
    """A local directory the LLM authored/tested (ADR §3.2's primary daily
    "promote" loop) or a hand-written plugin already on disk. Middle RCE
    trust risk — already on the operator's own machine."""
    kind: Literal["local"] = "local"
    path: str


class PluginSourceGit(BaseModel):
    """A remote git URL (ADR §3.8, extensible to `registry` etc. later).
    Highest RCE trust risk — fetching + then running remote code is an
    explicit operator-trust decision, never auto-run (§3.10)."""
    kind: Literal["git"] = "git"
    url: str


PluginSource = Annotated[
    Union[PluginSourceBuiltin, PluginSourceLocal, PluginSourceGit],
    Field(discriminator="kind"),
]


class PluginInstallIROp(BaseModel):
    """Promote/install a plugin (ADR 0064 §3.2/§3.8): resolve `source` → a
    source directory, copy it to `~/.reyn/plugins/<name>/`, expand
    `${REYN_*}` stable-location tokens (P1 `reyn.plugins.tokens`), validate
    its `plugin.json` manifest (P1 `reyn.plugins.manifest`),
    materialise any per-plugin runtime deps (§3.11 — install-time network
    fetch, spawn stays network-free), then register whatever capabilities
    the manifest declares by calling the EXISTING `skill_install` /
    `pipeline_install` handlers + a local mcp-registry write (§3.2: "the
    same copy → expand → register mechanism", never a fourth registry).

    `name` overrides the manifest's own `name` as the install directory /
    registry-provenance key; when unset, the manifest's `name` is used.
    """
    kind: Literal["plugin_install"]
    source: PluginSource
    name: str | None = None


class PluginUninstallIROp(BaseModel):
    """Inverse of `plugin_install` (ADR §3.9): drop every `.reyn/config/
    {mcp,pipelines,skills}.yaml` entry tagged `plugin_id == name` (§3.7),
    THEN remove the `~/.reyn/plugins/<name>/` copy — drop-registry-first so
    an interrupted uninstall never leaves a live registry entry pointing at
    a deleted copy (§3.11)."""
    kind: Literal["plugin_uninstall"]
    name: str


class EmitHookEventIROp(BaseModel):
    """Emit an LLM-authored hook-event onto this session's ``HookBus`` (Hook-Event
    Redesign Phase 5 part 2, proposal ``docs/deep-dives/proposals/
    0059-hook-event-redesign.md`` §8/§8.4).

    **Normal (tool-facing) use**: set only ``event_name`` (+ optional
    ``payload``). The emitted kind is ALWAYS ``llm:<session_id>:<event_name>``
    — the session component is supplied ONLY by ``OpContext.session_id`` at
    handler-execution time (structural session-binding, §8.4 item 3): the
    router tool schema (``reyn.tools.emit_hook_event``) exposes ONLY
    ``event_name``/``payload``, never a session, so a well-behaved LLM tool
    call cannot even express a foreign session.

    ``target_kind`` is a **defense-in-depth escape hatch, deliberately NOT
    exposed in the router tool's JSON schema** (an LLM function-call cannot
    set it through the normal path). It exists so (a) the OUT-set kind
    whitelist (``reyn.hooks.schema_registry.is_emittable_llm_kind``) has a
    REAL, exercisable subject — proposal §8.4 item 3 requires the whitelist
    be an enforced reject, not a dormant check that can never actually fire
    — and (b) any OTHER caller of this Op model (e.g. a future Control-IR
    surface) is held to the exact same gate. The handler validates
    ``target_kind`` (when set) through the SAME ``is_emittable_llm_kind``
    whitelist BEFORE ``HookBus.publish`` — a non-self-session or
    non-``llm:*`` ``target_kind`` (``builtin:*``/``composed:*``/
    ``webhook:*``/``mcp:*``, or another session's ``llm:*``) is REJECTED,
    never reaches the bus, and is never used to route anywhere (this
    OpContext's ``hook_bus`` is a single fixed reference — there is no
    lookup-by-session-id path for the handler to route a mismatched
    ``target_kind`` to a different session's bus even if the whitelist were
    absent). ``composed:*`` in particular must never be LLM-forgeable — a
    forged ``composed:*`` event would fire a composed-only hook without the
    correlation logic a real Composer enforces before publishing
    (anti-spoofing, proposal §1/§2).

    ``event_name`` is schema-constrained (#2890 F6): a ``pattern`` restricts
    it to ``[A-Za-z0-9_.-]+`` and a ``max_length`` caps it, so control
    characters / newlines / unbounded length can never flow into the
    constructed ``kind`` (and from there into the P6 audit-event / anything
    that renders ``kind`` for display) even though the kind is already
    structurally confined to this session's own ``llm:{session_id}:`` prefix
    (no namespace escape possible either way — this is defense-in-depth on
    top of that confinement, not a new security boundary)."""

    kind: Literal["emit_hook_event"]
    event_name: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]*$", max_length=200)
    target_kind: str | None = None
    payload: dict = Field(default_factory=dict)


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
#
# FP-0057 Phase 1: EmbedIROp is RE-ADDED — #1303's rationale (no caller) is
# obsolete (the skill engine that motivated the collapse is deleted, #2438).
# `embed` is now the exposed USER-FACING primitive (user composes embed -> an
# external MCP vector-DB via pipeline) AND the shared logic Phase 2's
# `index_update`/`semantic_search` call internally — same EmbeddingProvider,
# no duplicated embed logic, split only by audience surface.
#
# FP-0057 Phase 2a: `recall` is renamed `semantic_search` (clean-break — fixes
# the observed `recall`/`search_actions`/`memory` naming collision) and its
# query-embed call is rewired onto the multi-model-correct per-source-model
# grouping below (see `op_runtime/semantic_search.py`). `index_update` is
# NEW — incremental/delta-reconcile ingestion into a source's `IndexBackend`,
# calling the `embed` op internally (no duplicated embed logic).


class EmbedIROp(BaseModel):
    """Raw embedding primitive — batch text -> vectors (FP-0057 Phase 1).

    Batch-granular (list in -> list out); the `EmbeddingProvider` handles
    internal batching (`embedding.batch_size` config, default 100) — this
    op owns none of that, it is a thin typed envelope over the existing
    provider.

    Default-ALLOW (compute op, cost = the embedding API/compute, not a
    workspace write). LLM-callable via ToolDefinition `embed`, and therefore
    individually name-gateable by a per-session contextual narrowing at the
    RouterLoop gate (`effective.tool_contextually_denied`).
    """
    kind: Literal["embed"]
    texts: list[str]
    embedding_model: str = "standard"  # model class name or literal provider model id


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


class SemanticSearchIROp(BaseModel):
    """Macro op: per-source-model embed query → iterate index_query → merge
    top-K (ADR-0033 §2.1; FP-0057 Phase 2a renamed from `recall` — clean
    break, fixes the observed `recall`/`search_actions`/`memory` naming
    collision).

    **Multi-model correctness**: each source's embedding model is
    AUTO-ADOPTED from its recorded index model (`SourceManifest.
    embedding_model`, falling back to the `IndexBackend.stat()` value —
    never caller-supplied per-source). Sources are grouped by distinct
    model; the query is embedded ONCE per distinct model, and each source is
    queried with its matching model's vector. Cosine scores from DIFFERENT
    embedding spaces are never directly compared (they are not
    commensurable) — merging happens WITHIN a model group by score; across
    groups results combine by an order-preserving interleave, never by
    comparing raw score magnitudes cross-model. `embedding_model` is a
    fallback default used ONLY for a source with no recorded model yet
    (empty/unindexed source, where `index_query` falls back to
    enumeration anyway).

    Handler dispatches sub-ops via the OS dispatch path (= iterate op
    precedent). NOT LLM-callable: there is no `semantic_search`
    ToolDefinition — the agent-facing layer-1 RAG tool of that name was
    RETIRED by FP-0066 P1b (#3257) as a pre-audience-split relic. This op
    is the OS-internal substrate that tool used to ride, reached only
    through the op_runtime dispatch path.
    """
    kind: Literal["semantic_search"]
    query: str
    sources: list[str]                     # required, no default
    top_k: int = 5
    filters: dict[str, str] = Field(default_factory=dict)
    embedding_model: str = "standard"      # fallback only — see docstring


class IndexDropIROp(BaseModel):
    """Remove an indexed source entirely (ADR-0033 §2.1).

    Permission gate: the caller must hold `file.write` over the source
    manifest (`.reyn/config/index/sources.yaml`) plus the source's own index
    dir, and the destructive delete self-gates at the real write site inside
    the backend. The bool-axis `permissions.index_drop` / `require_index_drop`
    ask-prompt this docstring used to describe was removed by the #571
    permission-collapse arc (Phase 5) — see `op_runtime/index_drop.py`.

    NOT LLM-callable: there is no `drop_source` ToolDefinition — the
    agent-facing layer-1 RAG tool of that name was RETIRED by FP-0066 P1b
    (#3257). This op is the OS-internal substrate it used to ride.
    """
    kind: Literal["index_drop"]
    source: str


class IndexUpdateIROp(BaseModel):
    """Incremental / delta-reconcile ingestion into a source's `IndexBackend`
    (FP-0057 Phase 2a). NO full-rebuild mode — a from-scratch rebuild is
    `index_drop` -> `index_update` on an empty source.

    The caller (a chunker — the Chunker registry itself is out of scope
    here, Phase 2b/3) supplies `chunks`; each chunk carries a
    `content_hash` + `source_path` in its `metadata`. Reconciled against the
    source's CURRENT index, keyed by `content_hash` within each
    `source_path` (content-addressed, chunk-dedup semantics — same
    `content_hash` key `SqliteIndexBackend` already dedups on):

    - **add**: `content_hash` not in the index, `source_path` not
      previously indexed -> embed (via the `embed` op — same primitive, no
      duplicated embed logic) + insert.
    - **update**: `content_hash` not in the index, but `source_path` WAS
      previously indexed (content changed under a path this call
      re-supplies) -> embed the new chunk + insert; the path's now-stale
      hash(es) are removed in the same reconciliation pass.
    - **remove**: an indexed `content_hash` whose `source_path` IS among
      this call's `chunks` but whose hash is NOT among this call's
      `content_hash`es -> deleted. Reconciliation is scoped to the
      `source_path`s THIS call supplies chunks for — a `source_path` this
      call never mentions is left untouched (a partial re-ingest of a few
      files never mass-deletes the rest of the source).
    - **skip**: `content_hash` already indexed -> no-op (no re-embed, no
      write).

    **Source-model-bound**: the source's embedding model is recorded on
    first ingestion and reused on every subsequent `index_update` for that
    source (an existing source's `embedding_model` field is authoritative
    over a caller-supplied override — a source is one embedding space).

    Permission gate: additive/own-write (not destructive), so it declares
    `file.write` over this source's own index + the sources.yaml manifest and
    nothing more — same shape as `index_query`'s read gate and `index_drop`'s
    write gate, and constrained by a sandbox `write_paths` cap. There is no
    ask-prompt on either this op or `index_drop` (the bool-axis
    `require_index_drop` was removed by the #571 permission-collapse arc,
    Phase 5), and the default-ALLOW posture no longer comes from a
    ToolDefinition's `ToolGates` — see the next paragraph.

    NOT LLM-callable: there is no `index_update` ToolDefinition — the
    agent-facing layer-1 RAG tool of that name was RETIRED by FP-0066 P1b
    (#3257). This op is the OS-internal substrate it used to ride.
    """
    kind: Literal["index_update"]
    source: str
    chunks: list[dict[str, Any]] = Field(default_factory=list)  # {text, metadata: {content_hash, source_path, ...}}
    embedding_model: str = "standard"  # used only when the source has no recorded model yet
    description: str | None = None     # SourceManifest description (first-index / override)
    path: str | None = None            # SourceManifest path (first-index / override)


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
    # op_runtime/mcp_read_resource.py + RouterHostAdapter.mcp_list_resources,
    # #3447 — folded off Session, still no op-kind of its own).
    "mcp_read_resource": MCPReadResourceIROp,
    # #2597 slice ②b: resource subscriptions — subscribe/unsubscribe are
    # permission-gated the same way read is (stateful action against the
    # server); the resulting push notification is an EventLog event
    # (mcp_resource_updated), not routed through the Op union at all.
    "mcp_subscribe_resource": MCPSubscribeResourceIROp,
    "mcp_unsubscribe_resource": MCPUnsubscribeResourceIROp,
    # #2597 slice ②c: prompts consumption — get is permission-gated (external
    # content); list stays op-kind-free, mirroring list_tools/list_resources (see
    # op_runtime/mcp_get_prompt.py + RouterHostAdapter.mcp_list_prompts, #3447 —
    # folded off Session, still no op-kind of its own).
    "mcp_get_prompt": MCPGetPromptIROp,
    "ask_user":    AskUserIROp,
    # FP-0054 PR-A: present bulk data + a declarative view to the user surface
    # without the data passing through LLM output tokens. Tier 0 (ask_user's
    # sibling); the only gate is data_ref read authority == file.read.
    "present":     PresentIROp,
    # FP-0055 PR-2: render a Jinja2 template against structured data → a string.
    # A sandboxed producer (no side effects, no sink); template_ref/data_ref read
    # authority == file.read.
    "render_template": RenderTemplateIROp,
    "web_fetch":   WebFetchIROp,
    "web_search":  WebSearchIROp,
    "mcp_install": MCPInstallIROp,
    # #1983: was registered + documented (control-ir.md) + handled but ABSENT
    # here → a phase emitting it failed Op union validation. Added to restore
    # the control-ir.md ↔ map sync invariant.
    "mcp_drop_server": MCPDropServerIROp,
    # FP-0057 Phase 1: raw embed primitive (user-facing; also Phase 2's shared
    # internal embed logic — no duplication, split by audience surface).
    "embed":       EmbedIROp,
    "index_query": IndexQueryIROp,
    # FP-0057 Phase 2a: `recall` renamed `semantic_search` (clean-break —
    # fixes the observed recall/search_actions/memory naming collision).
    "semantic_search": SemanticSearchIROp,
    "index_drop":  IndexDropIROp,
    # FP-0057 Phase 2a: incremental/delta-reconcile ingestion (add/update/
    # remove/skip against a source's IndexBackend). No full-rebuild mode.
    "index_update": IndexUpdateIROp,
    "sandboxed_exec": SandboxedExecIROp,
    "compact": CompactIROp,
    # #2548 PR-C: local skill directory install — register a SKILL.md dir into
    # skills.entries (parallel to mcp_install writing mcp.servers).
    "skill_install": SkillInstallIROp,
    # FP-0066 P0 (#3247): dedicated skill-activation verb — extracted OUT of
    # file.read's former is_skill_body_path special-case (ADR 0064 §3.5,
    # reversing the #2971 "no dedicated verb" drift).
    "load_skill": LoadSkillIROp,
    # pipeline install — register a pipeline DSL file into pipelines.entries
    # (mirrors skill_install writing skills.entries; parallel install mechanism).
    "pipeline_install": PipelineInstallIROp,
    # proposal 0060 Phase 1 Layer A (A8): register a named presentation template
    # into presentations.entries (mirrors skill_install/pipeline_install's
    # structure; lower threat — validate_blueprint IS the structural gate).
    "presentation_install": PresentationInstallIROp,
    # ADR 0064 plugin model P2: promote/install and uninstall a self-contained
    # plugin directory. Orchestrates skill_install/pipeline_install + a local
    # mcp-registry write — see PluginInstallIROp/PluginUninstallIROp docstrings.
    "plugin_install": PluginInstallIROp,
    "plugin_uninstall": PluginUninstallIROp,
    # Hook-Event Redesign Phase 5 part 2 (proposal 0059 §8): LLM-authored
    # hook-event emission onto the caller's own HookBus. See EmitHookEventIROp's
    # docstring for the structural session-binding + kind-whitelist security
    # discipline enforced by the handler (op_runtime/emit_hook_event.py).
    "emit_hook_event": EmitHookEventIROp,
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
            PresentIROp,
            RenderTemplateIROp,
            WebFetchIROp, WebSearchIROp, MCPInstallIROp,
            MCPDropServerIROp,
            EmbedIROp, IndexQueryIROp, SemanticSearchIROp, IndexDropIROp,
            IndexUpdateIROp,
            SandboxedExecIROp, CompactIROp,
            SkillInstallIROp,
            PipelineInstallIROp,
            PresentationInstallIROp,
            EmitHookEventIROp,
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
