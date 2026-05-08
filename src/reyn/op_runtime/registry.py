"""Op kind registry — single source of truth for Control IR op kinds.

This module centralises the three previously scattered op kind definitions:

  1. ``OP_KIND_MODEL_MAP``  (was ``_IROP_MODEL_MAP`` in kernel/control_ir_executor.py)
     Maps each op kind to its typed Pydantic IROp model.  Used by the control
     IR executor to derive tool-parameter JSON schemas.

  2. ``ALL_OP_KINDS``  (was ``_KNOWN_OP_KINDS`` in compiler/linter.py)
     Frozenset of every valid op kind.  Used by the DSL linter to flag
     misspelled ``allowed_ops`` entries.

  3. ``OP_PURITY``  (NEW in skill resume design)
     Determinism classification.  See ``OpPurity`` enum below.  Used by
     dispatch_tool to decide whether to emit step events for resume.

Consumers:
  - ``reyn.kernel.control_ir_executor``  — ``OP_KIND_MODEL_MAP``
  - ``reyn.compiler.linter``             — ``ALL_OP_KINDS``
  - ``reyn.dispatch.dispatcher``         — ``OP_PURITY`` (skip emission for ``pure``)

Note: ``_WRITE_OPS`` / ``_READ_OPS`` in ``op_runtime/file.py`` classify
*file sub-operations* (op.op values within the "file" kind), not top-level
op kinds.  They are a different concern and intentionally stay local.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from reyn.schemas.models import (
    AskUserIROp,
    FileIROp,
    LintIROp,
    MCPIROp,
    RunSkillIROp,
    ShellIROp,
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
    "file":       FileIROp,
    "mcp":        MCPIROp,
    "run_skill":  RunSkillIROp,
    "shell":      ShellIROp,
    "lint":       LintIROp,
    "ask_user":   AskUserIROp,
    "web_fetch":  WebFetchIROp,
    "web_search": WebSearchIROp,
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
    "lint":       OpPurity.pure,
    # World-state dependent (read APIs).  ``file`` includes both read and
    # write ops; we mark it side_effect (conservative).  Sub-op refinement
    # can happen inside the handler if perf demands.
    "web_fetch":  OpPurity.world,
    "web_search": OpPurity.world,
    # Side effect (workspace mutation; file/write/delete fall here).
    "file":       OpPurity.side_effect,
    # External / unknown side-effecting.
    "mcp":        OpPurity.external,
    "shell":      OpPurity.external,
    "run_skill":  OpPurity.external,
    # User interaction (state-changing for the user).
    "ask_user":   OpPurity.side_effect,
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
# Derived from the registry; the linter imports this to detect misspellings.
# ---------------------------------------------------------------------------

ALL_OP_KINDS: frozenset[str] = frozenset(OP_KIND_MODEL_MAP.keys())
