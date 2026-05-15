"""Op kind registry — coarse-name op classification (ADR-0026 Phase 4 steady state).

This module holds three classifications keyed by **coarse op kind**
(= the ``op.kind`` values phase Control IR emits today: ``file`` /
``mcp`` / ``run_skill`` / ``shell`` / ``lint`` / ``ask_user`` /
``web_fetch`` / ``web_search``):

  1. ``OP_KIND_MODEL_MAP`` — coarse name → IROp Pydantic model.
     Schema derivation is now done by ``reyn.tools.ToolRegistry``
     entries (= ADR-0026 Phase 4-3); this map is retained as a backwards-
     compat reference and a stable target for ``ALL_OP_KINDS``.

  2. ``ALL_OP_KINDS`` — frozenset of coarse op kinds.  Used by the DSL
     linter to flag misspelled ``allowed_ops`` entries; also drives
     ``OP_PURITY`` coverage tests.

  3. ``OP_PURITY`` — determinism classification.  See ``OpPurity`` enum
     below.  Used by ``dispatch_tool`` to decide whether to emit step
     events for resume.

Helper:
  - ``is_op_allowed(op_kind, allowed_ops)`` — prefix-wildcard membership
    check (= ADR-0026 Phase 4-2c).  ``allowed_ops: ["file"]`` matches
    fine-grained kinds (``read_file`` / ``write_file`` / etc.) when phase
    Control IR migrates to fine-grained ``op.kind`` values in a future
    phase.  Today phase emits coarse kinds, so the helper is a pass-
    through but the rule is in place to keep skill frontmatter stable.

Consumers:
  - ``reyn.compiler.linter``     — ``ALL_OP_KINDS``
  - ``reyn.dispatch.dispatcher`` — ``OP_PURITY`` (skip emission for ``pure``)
  - ``reyn.kernel.control_ir_executor`` — ``is_op_allowed`` for the
    ``allowed_ops`` filter

Note: ``_WRITE_OPS`` / ``_READ_OPS`` in ``op_runtime/file.py`` classify
*file sub-operations* (op.op values within the "file" kind), not top-level
op kinds.  They are a different concern and intentionally stay local.

Migration note (ADR-0026 Phase 4 closeout)
------------------------------------------
``ControlIRExecutor._build_phase_tool_catalog`` reads schema from
``get_default_registry()`` (= unified ToolRegistry) directly.  This
module's ``OP_KIND_MODEL_MAP`` no longer drives dispatch-time schema
derivation; it survives as the canonical coarse-kind list (= for purity
classification, linter warnings, prefix-wildcard mappings).  Removing
this module entirely awaits phase Control IR's migration to fine-grained
``op.kind`` values, at which point the linter and OP_PURITY can also
key off registry names.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from reyn.schemas.models import (
    AskUserIROp,
    EmbedIROp,
    FileIROp,
    IndexDropIROp,
    IndexQueryIROp,
    IndexWriteIROp,
    JudgeOutputIROp,
    LintIROp,
    MCPInstallIROp,
    MCPIROp,
    RecallIROp,
    RunSkillIROp,
    SandboxedExecIROp,
    ShellIROp,
    SkillResolveIROp,
    WebFetchIROp,
    WebSearchIROp,
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
    "file":        FileIROp,
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
    # World-state dependent (read APIs).  ``file`` includes both read and
    # write ops; we mark it side_effect (conservative).  Sub-op refinement
    # can happen inside the handler if perf demands.
    "web_fetch":   OpPurity.world,
    "web_search":  OpPurity.world,
    # Side effect (workspace mutation; file/write/delete fall here).
    "file":        OpPurity.side_effect,
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
    "file":      frozenset({"read_file", "write_file", "delete_file", "list_directory"}),
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
