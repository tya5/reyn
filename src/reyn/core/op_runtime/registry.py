"""Op kind registry — op kind classification (ADR-0026 Phase 4 steady state).

This module holds two classifications keyed by **op kind**
(= the ``op.kind`` values phase Control IR emits today:
``read_file`` / ``write_file`` / ``edit_file`` / ``delete_file`` /
``glob_files`` / ``grep_files`` / ``mcp`` / ``shell`` /
``ask_user`` / ``web_fetch`` / ``web_search`` / etc.).

Note: the coarse ``"file"`` kind was retired in #1240 Wave 2b. All file
ops now use fine kinds (``read_file`` / ``write_file`` / etc.).  The
execution backend (``op_runtime/file.py`` ``register("file", handle)``)
is KEPT — fine handlers still build ``FileIROp(kind="file")`` internally.

  1. ``OP_KIND_MODEL_MAP`` — op kind → IROp Pydantic model.
     **Relocated to ``reyn.schemas.models`` (#1983)** (co-located with IROp
     classes; registry re-imports ``ALL_OP_KINDS`` from there).
     Schema derivation is done by ``reyn.tools.ToolRegistry``
     entries (= ADR-0026 Phase 4-3); the map remains the stable target
     for ``ALL_OP_KINDS``.

  2. ``ALL_OP_KINDS`` — frozenset of op kinds.  Used by the DSL
     linter to flag misspelled ``allowed_ops`` entries.

Consumers:
  - ``reyn.core.compiler.linter``     — ``ALL_OP_KINDS``

Note (#2890 F9): a coarse→fine ``allowed_ops`` prefix-wildcard helper
(``is_op_allowed``/``is_op_instance_allowed``/``COARSE_TO_FINE``) used to
live in this module for ``reyn.core.kernel.control_ir_executor``'s
``allowed_ops`` filter. That consumer was removed in #2438 (#2434 stage3b —
kernel phase-engine bulk-delete, commit ``d3c8c7a1``); the helper was left
behind with zero
remaining call sites (verified: a full ``src/``/``tests/`` grep finds no
caller other than the two functions calling each other) and was removed
here as dead code rather than kept as an unreachable/untested surface.

Note: ``_WRITE_OPS`` / ``_READ_OPS`` in ``op_runtime/file.py`` classify
*file sub-operations* (op.op values within the "file" kind), not top-level
op kinds.  They are a different concern and intentionally stay local.
"""
from __future__ import annotations

# #1983: OP_KIND_MODEL_MAP + ALL_OP_KINDS relocated to schemas/models.py
# (co-located with the IROp model classes + the now-derived Op union =
# single source; the map lived here before, but registry imports those model
# classes, so models.py could not derive the union from the map without a cycle).
# registry re-imports ALL_OP_KINDS from models for ALL_TOOL_NAMES — an
# intentional op-runtime-view convenience, NOT a migration shim.
from reyn.schemas.models import ALL_OP_KINDS

# #1983: OP_KIND_MODEL_MAP + ALL_OP_KINDS now live in schemas/models.py — the
# single source (the Op discriminated union derives from the map there,
# completeness-by-construction). Add a new op kind in models.py; ALL_OP_KINDS is
# imported above (used by ALL_TOOL_NAMES below).

# ALL_OP_KINDS is imported from schemas/models.py above (single source, #1983);
# it remains importable from this module for back-compat (intentional convenience).


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
# The phase-advertised chat name "call_mcp_tool" aliases to the canonical
# execution op kind "mcp".  The phase frame shows the chat name so phase =
# chat-tools subset (catalog-axis goal); parse boundaries in op_loop +
# json-mode rewrite it to the op kind BEFORE Op validation.  The
# the allowed-ops filter applies this alias so allowed_ops=[mcp]
# matches the advertised call_mcp_tool spec.  The execution backend
# (op_runtime/mcp.py) and Op model (MCPIROp) use the op-kind name.
# ---------------------------------------------------------------------------

_PHASE_TOOL_NAME_ALIAS: dict[str, str] = {
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
