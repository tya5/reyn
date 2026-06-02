"""Op kind registry — op kind classification (ADR-0026 Phase 4 steady state).

This module holds three classifications keyed by **op kind**
(= the ``op.kind`` values phase Control IR emits today:
``read_file`` / ``write_file`` / ``edit_file`` / ``delete_file`` /
``glob_files`` / ``grep_files`` / ``mcp`` / ``run_skill`` / ``shell`` /
``lint`` / ``ask_user`` / ``web_fetch`` / ``web_search`` / etc.).

Note: the coarse ``"file"`` kind was retired in #1240 Wave 2b. All file
ops now use fine kinds (``read_file`` / ``write_file`` / etc.).  The
execution backend (``op_runtime/file.py`` ``register("file", handle)``)
is KEPT — fine handlers still build ``FileIROp(kind="file")`` internally.

  1. ``OP_KIND_MODEL_MAP`` — op kind → IROp Pydantic model.
     Schema derivation is done by ``reyn.tools.ToolRegistry``
     entries (= ADR-0026 Phase 4-3); this map is retained as a
     stable target for ``ALL_OP_KINDS`` and purity classification.

  2. ``ALL_OP_KINDS`` — frozenset of op kinds.  Used by the DSL
     linter to flag misspelled ``allowed_ops`` entries; also drives
     ``OP_PURITY`` coverage tests.

  3. ``OP_PURITY`` — determinism classification.  See ``OpPurity`` enum
     below.  Used by ``dispatch_tool`` to decide whether to emit step
     events for resume.

Helper:
  - ``is_op_allowed(op_kind, allowed_ops)`` — prefix-wildcard membership
    check (= ADR-0026 Phase 4-2c).  ``COARSE_TO_FINE`` covers ``mcp``
    and ``run_skill`` coarse→fine mappings (prefix-wildcard).

Consumers:
  - ``reyn.compiler.linter``     — ``ALL_OP_KINDS``
  - ``reyn.dispatch.dispatcher`` — ``OP_PURITY`` (skip emission for ``pure``)
  - ``reyn.kernel.control_ir_executor`` — ``is_op_allowed`` for the
    ``allowed_ops`` filter

Note: ``_WRITE_OPS`` / ``_READ_OPS`` in ``op_runtime/file.py`` classify
*file sub-operations* (op.op values within the "file" kind), not top-level
op kinds.  They are a different concern and intentionally stay local.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from reyn.schemas.models import (
    AskUserIROp,
    CompactIROp,
    DeleteFileIROp,
    EditFileIROp,
    EmbedIROp,
    GlobFilesIROp,
    GrepFilesIROp,
    IndexDropIROp,
    IndexQueryIROp,
    IndexWriteIROp,
    JudgeOutputIROp,
    LintIROp,
    MCPInstallIROp,
    MCPIROp,
    ReadFileIROp,
    RecallIROp,
    RunSkillIROp,
    SandboxedExecIROp,
    ShellIROp,
    SkillResolveIROp,
    WebFetchIROp,
    WebSearchIROp,
    WriteFileIROp,
)


class OpPurity(str, Enum):
    """Determinism classification for ops, used by skill resume design.

    - ``pure``: same args → same result, no external state, no side effects.
      Resume can re-execute safely; step event emission is **skipped** to
      reduce WAL volume.
    - ``world``: depends on external state (filesystem, network reads),
      but no side effects.  Result varies across calls; step event must
      record the result so resume uses cached value, not re-execute.
    - ``side_effect``: writes to local state (workspace, filesystem).
      Both ``step_started`` and ``step_completed`` are emitted so a crash
      between them surfaces as an ambiguous state on resume.
    - ``external``: invokes external systems with potential side effects
      (mcp/call_tool, shell, run_skill).  Same emission policy as
      ``side_effect``; the distinction is audit metadata.
    - ``llm``: language-model call.  Cost side effect + non-deterministic
      output.  ``step_completed`` records the output; ``step_started`` is
      not emitted (no externally-observable side effect to disambiguate).
    """

    pure = "pure"
    world = "world"
    side_effect = "side_effect"
    external = "external"
    llm = "llm"


# ---------------------------------------------------------------------------
# OP_KIND_MODEL_MAP
# ---------------------------------------------------------------------------
# Maps op kind → IROp Pydantic model used for JSON schema derivation.
# Add a new entry here whenever a new op kind is introduced — the kernel
# executor and DSL linter both derive from this single source.
# ---------------------------------------------------------------------------

OP_KIND_MODEL_MAP: dict[str, type[BaseModel]] = {
    # #1240 Wave 2b: coarse "file" kind dropped from OP_KIND_MODEL_MAP.
    # The execution backend (op_runtime/file.py register("file") + handle())
    # is KEPT — fine handlers still build FileIROp(kind="file") internally.
    # #1240 Wave 1: fine-grained file kinds (phase = chat-tools subset).
    # control_ir_executor routes each via the registry (READ_FILE/WRITE_FILE/...
    # ToolDefinitions, gates.phase=allow) — no separate op_runtime handler.
    "read_file":   ReadFileIROp,
    "write_file":  WriteFileIROp,
    "edit_file":   EditFileIROp,
    "delete_file": DeleteFileIROp,
    # #1240 Wave 1.5: glob_files / grep_files fine kinds (same pattern).
    # GLOB_FILES/GREP_FILES ToolDefinitions (tools/file.py, gates.phase=allow)
    # route via the unified registry — same handler path chat uses.
    "glob_files":  GlobFilesIROp,
    "grep_files":  GrepFilesIROp,
    "mcp":         MCPIROp,
    "run_skill":   RunSkillIROp,
    "shell":       ShellIROp,
    "lint":        LintIROp,
    "ask_user":    AskUserIROp,
    "web_fetch":   WebFetchIROp,
    "web_search":  WebSearchIROp,
    "mcp_install": MCPInstallIROp,
    # ADR-0033: RAG-extensible OS — embed / index_* / recall ops
    "embed":       EmbedIROp,
    "index_write": IndexWriteIROp,
    "index_query": IndexQueryIROp,
    "recall":      RecallIROp,
    "index_drop":  IndexDropIROp,
    # FP-0017: sandboxed_exec — argv under SandboxPolicy via SandboxBackend.
    "sandboxed_exec": SandboxedExecIROp,
    # FP-0007 Component D: LLM-based output scorer for in-phase eval loops.
    "judge_output": JudgeOutputIROp,
    # R-PURE-MODE Wave 5a: resolve a skill name to its on-disk path.
    "skill_resolve": SkillResolveIROp,
    # #272/#1128: LLM-emittable voluntary compaction (advisory; the mandatory
    # retry_loop backstop is independent).
    "compact": CompactIROp,
}

# ---------------------------------------------------------------------------
# OP_PURITY
# ---------------------------------------------------------------------------
# Determinism classification per op kind.  Default is ``side_effect`` (safe
# side: emit both events) for any kind not explicitly listed.  Sub-types
# (e.g. file/read vs file/write) cannot be distinguished here; for kinds
# whose behavior varies by sub-op (file, mcp), ``side_effect`` is chosen
# as the conservative default and finer-grained classification belongs
# inside the op handler.
# ---------------------------------------------------------------------------

OP_PURITY: dict[str, OpPurity] = {
    # Pure: pure computation, no I/O.
    "lint":        OpPurity.pure,
    # World-state dependent (read APIs).
    "web_fetch":   OpPurity.world,
    "web_search":  OpPurity.world,
    # Side effect (workspace mutation; file write/delete fall here).
    # #1240 Wave 2b: coarse "file" purity entry dropped (coarse kind removed).
    # #1240 Wave 1: fine-grained file kinds. Conservative side_effect stance
    # (matches the former coarse "file" classification). A read_file→world
    # accuracy refinement is a separate decision, out of pivot scope.
    "read_file":   OpPurity.side_effect,
    "write_file":  OpPurity.side_effect,
    "edit_file":   OpPurity.side_effect,
    "delete_file": OpPurity.side_effect,
    # #1240 Wave 1.5: glob_files / grep_files. Same conservative stance as
    # Wave 1 fine kinds — match the coarse "file" side_effect classification.
    "glob_files":  OpPurity.side_effect,
    "grep_files":  OpPurity.side_effect,
    # External / unknown side-effecting.
    "mcp":         OpPurity.external,
    "shell":       OpPurity.external,
    "run_skill":   OpPurity.external,
    # User interaction (state-changing for the user).
    "ask_user":    OpPurity.side_effect,
    # MCP server install: writes config + secrets, runs registry fetch.
    "mcp_install": OpPurity.side_effect,
    # ADR-0033 RAG ops:
    # - embed: external API call (LiteLLM passthrough), token cost.
    "embed":       OpPurity.external,
    # - index_write: writes to backend SQLite / future plugins.
    "index_write": OpPurity.side_effect,
    # - index_query: read-only, depends on backend state (= world).
    "index_query": OpPurity.world,
    # - recall: macro op dispatching embed + index_query, treated as external
    #   (sub-ops emit their own events for trace fidelity).
    "recall":      OpPurity.external,
    # - index_drop: deletes backend collection + manifest entry.
    "index_drop":  OpPurity.side_effect,
    # FP-0017: sandboxed_exec — same external side-effect class as shell.
    "sandboxed_exec": OpPurity.external,
    # FP-0007 Component D: LLM call with token cost side effect.
    "judge_output": OpPurity.llm,
    # R-PURE-MODE Wave 5a: read-only path resolution, no external API calls.
    "skill_resolve": OpPurity.world,
    # #272/#1128: compact triggers a compaction LLM call (cost) AND mutates
    # history/state; like `recall` it is a macro whose inner compaction engine
    # emits its own events. external = emit both started+completed so a crash
    # mid-compaction surfaces as ambiguous on resume.
    "compact": OpPurity.external,
}


def get_op_purity(op_kind: str) -> OpPurity:
    """Return the purity classification for an op kind.

    Defaults to ``side_effect`` (conservative — emit both step events) for
    unknown kinds.  Future skill resume dispatcher uses this to decide
    whether to emit ``tool_called`` ahead of execution and whether to
    record the result in ``tool_returned``.
    """
    return OP_PURITY.get(op_kind, OpPurity.side_effect)


# ---------------------------------------------------------------------------
# ALL_OP_KINDS
# ---------------------------------------------------------------------------
# Coarse-name set (= OP_KIND_MODEL_MAP.keys()).  These are the kinds phase
# Control IR emits in ``op.kind`` today, the names ``allowed_ops``
# frontmatter targets, and the rows OP_PURITY classifies.  Fine-grained
# router-side names (= read_file / write_file / call_mcp_tool / etc.) are
# NOT in this set; they live in the unified ToolRegistry (reyn.tools).
# When phase Control IR migrates to fine-grained kinds in a future phase,
# this set will expand to a union of coarse + fine.  Until then, the
# ``is_op_allowed`` helper below covers the prefix-wildcard semantics for
# any fine-grained kinds the future phase emits.
# ---------------------------------------------------------------------------

ALL_OP_KINDS: frozenset[str] = frozenset(OP_KIND_MODEL_MAP.keys())


# ---------------------------------------------------------------------------
# Coarse → fine prefix-wildcard mapping (ADR-0026 Phase 4)
# ---------------------------------------------------------------------------
# Skill frontmatter conventionally declares ``allowed_ops: [file]`` — a
# coarse name that originally matched ``op.kind == "file"`` 1:1.  As
# router-side fine-grained names (= read_file / write_file / etc.) become
# canonical phase-side too, the coarse declaration must continue to match
# the fine-grained ops by prefix wildcard.  ``is_op_allowed`` consults
# this map to keep existing skills working without frontmatter migration.
# ---------------------------------------------------------------------------

COARSE_TO_FINE: dict[str, frozenset[str]] = {
    # #1240 Wave 2b: "file" entry removed — coarse kind dropped. Skills that
    # still declare allowed_ops: [file] will fail linting (ALL_OP_KINDS no
    # longer includes "file") and should migrate to fine kinds.
    "mcp":       frozenset({"call_mcp_tool", "list_mcp_servers", "list_mcp_tools"}),
    "run_skill": frozenset({"invoke_skill"}),
}


def is_op_allowed(op_kind: str, allowed_ops: set[str] | frozenset[str]) -> bool:
    """Return True if ``op_kind`` is permitted by the ``allowed_ops`` set.

    Membership rules (= ADR-0026 Phase 4):

    1. **Direct match** — ``op_kind in allowed_ops`` (= the legacy 1:1
       semantics that all existing skill frontmatter relies on).
    2. **Prefix-wildcard** — when ``allowed_ops`` contains a coarse name
       (e.g. ``"file"``) and ``op_kind`` is a fine-grained name covered
       by that coarse (e.g. ``"read_file"``), the op is allowed.

    This forward-looking helper keeps existing ``allowed_ops: [file]``
    declarations working as Control IR migrates to fine-grained kinds in
    later phases.  Today (post Phase 4-2a) phase Control IR still emits
    coarse ``op.kind`` values, so rule 2 is exercised only by tests and
    by future migrations.
    """
    if op_kind in allowed_ops:
        return True
    for coarse in allowed_ops:
        fine_set = COARSE_TO_FINE.get(coarse)
        if fine_set is not None and op_kind in fine_set:
            return True
    return False


# ---------------------------------------------------------------------------
# split_tool_name — general helper (KEPT)
# ---------------------------------------------------------------------------
# split_tool_name is a general utility used by op_loop + conversion paths.
# FILE_VERB_TOOL_NAMES / op_tool_name / is_op_instance_allowed were the D7
# file-verb-granular machinery (#1212 PR4); they are retired in #1240 Wave 2b
# now that the coarse "file" kind is dropped and all file ops use fine kinds
# (read_file/write_file/edit_file/delete_file/glob_files/grep_files).
# ---------------------------------------------------------------------------

# Every name the linter / catalog may legitimately see in allowed_ops.
# Fine file kinds are already in ALL_OP_KINDS (they're in OP_KIND_MODEL_MAP).
ALL_TOOL_NAMES: frozenset[str] = ALL_OP_KINDS


# ---------------------------------------------------------------------------
# _PHASE_TOOL_NAME_ALIAS — chat-name → op-kind mapping (#1240 Wave 2b)
# ---------------------------------------------------------------------------
# Phase-advertised chat names ("invoke_skill" / "call_mcp_tool") alias to the
# canonical execution op kinds ("run_skill" / "mcp").  The phase frame shows
# the chat names so phase = chat-tools subset (catalog-axis goal); parse
# boundaries in op_loop + json-mode rewrite them to the op kind BEFORE
# ControlIROp validation.  The build_frame allowed-ops filter applies this
# alias so allowed_ops=[run_skill] matches the advertised invoke_skill spec.
# Execution backends (op_runtime/run_skill.py, op_runtime/mcp.py) and
# ControlIROp models (RunSkillIROp, MCPIROp) are UNCHANGED — they continue
# to use the op-kind names.
# ---------------------------------------------------------------------------

_PHASE_TOOL_NAME_ALIAS: dict[str, str] = {
    "invoke_skill": "run_skill",
    "call_mcp_tool": "mcp",
}


def split_tool_name(tool_name: str) -> tuple[str, str | None]:
    """Split a tool-name into ``(kind, verb)``.

    Legacy file-verb names (``"file__read"``) → ``("file", "read")``.
    Plain kind names (``"read_file"``, ``"web_fetch"``) → ``(name, None)``.

    The ``"file__"`` prefix branch handles any surviving ``file__*`` tool-call
    names from in-flight op-loop sessions (= backward compat during rollout).
    New sessions emit fine kind names directly (``read_file`` etc.).
    """
    if tool_name.startswith("file__"):
        return "file", tool_name[len("file__"):]
    return tool_name, None


def is_op_instance_allowed(op: object, allowed_ops: set[str] | frozenset[str]) -> bool:
    """Gate a ControlIROp instance against an ``allowed_ops`` set.

    Delegates to ``is_op_allowed(op.kind, allowed_ops)`` for all op kinds.
    The D7 file-verb-granular special-case (``file__read``-style) is retired
    in #1240 Wave 2b now that the coarse ``file`` kind is dropped; all file ops
    use fine kinds (``read_file`` / ``write_file`` / etc.) in allowed_ops.
    """
    kind = getattr(op, "kind", None) or ""
    return is_op_allowed(kind, allowed_ops)
