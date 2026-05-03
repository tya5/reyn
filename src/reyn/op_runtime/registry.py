"""Op kind registry — single source of truth for Control IR op kinds.

This module centralises the three previously scattered op kind definitions:

  1. ``OP_KIND_MODEL_MAP``  (was ``_IROP_MODEL_MAP`` in kernel/control_ir_executor.py)
     Maps each op kind to its typed Pydantic IROp model, or ``None`` when the
     kind has no dedicated model (tool, subagent).  Used by the control IR
     executor to derive tool-parameter JSON schemas.

  2. ``ALL_OP_KINDS``  (was ``_KNOWN_OP_KINDS`` in compiler/linter.py)
     Frozenset of every valid op kind.  Used by the DSL linter to flag
     misspelled ``allowed_ops`` entries.

Consumers:
  - ``reyn.kernel.control_ir_executor``  — ``OP_KIND_MODEL_MAP``
  - ``reyn.compiler.linter``             — ``ALL_OP_KINDS``

Note: ``_WRITE_OPS`` / ``_READ_OPS`` in ``op_runtime/file.py`` classify
*file sub-operations* (op.op values within the "file" kind), not top-level
op kinds.  They are a different concern and intentionally stay local.
"""
from __future__ import annotations
from typing import Type

from reyn.schemas.models import (
    FileIROp,
    MCPIROp,
    RunSkillIROp,
    ShellIROp,
    LintIROp,
    AskUserIROp,
    WebFetchIROp,
    WebSearchIROp,
)
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# OP_KIND_MODEL_MAP
# ---------------------------------------------------------------------------
# Maps op kind → IROp Pydantic model used for JSON schema derivation.
# ``None`` means the kind is valid but has no typed model (no arg validation).
# Add a new entry here whenever a new op kind is introduced — the kernel
# executor and DSL linter both derive from this single source.
# ---------------------------------------------------------------------------

OP_KIND_MODEL_MAP: dict[str, Type[BaseModel] | None] = {
    "file":       FileIROp,
    "mcp":        MCPIROp,
    "run_skill":  RunSkillIROp,
    "shell":      ShellIROp,
    "lint":       LintIROp,
    "ask_user":   AskUserIROp,
    "web_fetch":  WebFetchIROp,
    "web_search": WebSearchIROp,
    # Kinds with no dedicated typed model (handled generically):
    "tool":     None,
    "subagent": None,
}

# ---------------------------------------------------------------------------
# ALL_OP_KINDS
# ---------------------------------------------------------------------------
# Derived from the registry; the linter imports this to detect misspellings.
# ---------------------------------------------------------------------------

ALL_OP_KINDS: frozenset[str] = frozenset(OP_KIND_MODEL_MAP.keys())
