"""Tier 2: file_* ToolDefinition M3 Wave 2 invariants (ADR-0026 M3 Wave 2).

Verifies that READ_FILE, WRITE_FILE, DELETE_FILE, and LIST_DIRECTORY
ToolDefinitions:
- Produce byte-identical description/parameters output to the prior ToolSpec
  literals in router_tools.py (C1-C4 block). Drift would invalidate replay
  fixtures and change LLM tool affordance.
- Have gates.router="allow" and gates.phase="allow" (both surfaces allowed).
- Have the correct purity: read_only for read_file / list_directory,
  side_effect for write_file / delete_file.
- Have category="io" for all four.
- Are findable via ToolRegistry round-trip for both router and phase.
- Module-level description/parameter constants match ToolDefinition fields.

No mocks of collaborators. All tests use real ToolDefinition instances.
No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.file import (
    _DELETE_FILE_DESCRIPTION,
    _DELETE_FILE_PARAMETERS,
    _LIST_DIRECTORY_DESCRIPTION,
    _LIST_DIRECTORY_PARAMETERS,
    _READ_FILE_DESCRIPTION,
    _READ_FILE_PARAMETERS,
    _WRITE_FILE_DESCRIPTION,
    _WRITE_FILE_PARAMETERS,
    DELETE_FILE,
    LIST_DIRECTORY,
    READ_FILE,
    WRITE_FILE,
)
from reyn.tools.registry import ToolRegistry

# ── 1. LIST_DIRECTORY render_for_router byte-identity ───────────────────────

def test_list_directory_router_render_exact_description():
    """Tier 2: LIST_DIRECTORY description is byte-identical to the legacy ToolSpec
    description in router_tools.py C1 block. Any whitespace or punctuation diff
    is a stop signal that would drift LLMReplay fixtures."""
    rendered = LIST_DIRECTORY.render_for_router()
    legacy_description = (
        "List contents of a directory under the agent's read scope. "
        "Returns names + types (file/dir)."
    )
    assert rendered["function"]["description"] == legacy_description


def test_list_directory_router_render_exact_parameters():
    """Tier 2: LIST_DIRECTORY parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py C1 block."""
    rendered = LIST_DIRECTORY.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. READ_FILE render_for_router byte-identity ─────────────────────────────

def test_read_file_router_render_exact_description():
    """Tier 2: READ_FILE description is byte-identical to the legacy ToolSpec
    description in router_tools.py C2 block. Any whitespace or punctuation diff
    is a stop signal that would drift LLMReplay fixtures."""
    rendered = READ_FILE.render_for_router()
    legacy_description = (
        "Read a file's contents under the agent's read scope. "
        "Common conventions: README is at project root as "
        "`README.md`. CLAUDE.md, CHANGELOG.md, and "
        "configuration files (e.g. `reyn.yaml`, "
        "`pyproject.toml`) are at project root. Try these "
        "conventional paths directly instead of asking the "
        "user where the file lives."
    )
    assert rendered["function"]["description"] == legacy_description


def test_read_file_router_render_exact_parameters():
    """Tier 2: READ_FILE parameters schema pins the LLM-visible shape — ``path``
    is required, optional ``offset`` / ``limit`` expose the line-slice
    capability that already exists in ``op_runtime/file.py``. This shape is
    the read-side symmetry contract shared with ``reyn_src_read`` and
    ``read_memory_body``; widening it should be a deliberate cross-surface
    decision, not a drift."""
    rendered = READ_FILE.render_for_router()
    expected_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {
                "type": "integer",
                "description": (
                    "Line number to start reading from (0-indexed). "
                    "Omit to start at the beginning of the file."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Number of lines to read from `offset`. "
                    "Omit to read through end of file."
                ),
            },
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == expected_parameters


# ── 3. WRITE_FILE render_for_router byte-identity ────────────────────────────

def test_write_file_router_render_exact_description():
    """Tier 2: WRITE_FILE renders its exact description through render_for_router.

    Frozen drift-guard: a description change must update this pin AND re-check
    replay fixtures (the rendered tools[] payload is what the LLM sees). #187
    STEP 1 re-froze it after adding the reciprocal file__edit cross-ref.
    """
    rendered = WRITE_FILE.render_for_router()
    expected_description = (
        # #1625: reworded scheme-agnostic (WHAT not HOW) — was
        # "describe_action(...) for its args, then invoke_action" (the universal-
        # wrapper idiom leaking into the rendered code-API catalog, P7/P8).
        "Write content to a file under the agent's write scope. "
        "Creates or overwrites the WHOLE file. For a partial or surgical "
        "change to an existing file, prefer the `file__edit` action instead of "
        "rewriting the whole file."
    )
    assert rendered["function"]["description"] == expected_description


def test_write_file_router_render_exact_parameters():
    """Tier 2: WRITE_FILE parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py C3 block."""
    rendered = WRITE_FILE.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 4. DELETE_FILE render_for_router byte-identity ───────────────────────────

def test_delete_file_router_render_exact_description():
    """Tier 2: DELETE_FILE description is byte-identical to the legacy ToolSpec
    description in router_tools.py C4 block."""
    rendered = DELETE_FILE.render_for_router()
    legacy_description = (
        "Delete a file under the agent's write scope."
    )
    assert rendered["function"]["description"] == legacy_description


def test_delete_file_router_render_exact_parameters():
    """Tier 2: DELETE_FILE parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py C4 block."""
    rendered = DELETE_FILE.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 5. Gate invariants ────────────────────────────────────────────────────────

def test_read_file_gates_router_allow_phase_allow():
    """Tier 2: READ_FILE has gates.router="allow" and gates.phase="allow".
    File ops must be available to both router and phase callers."""
    assert READ_FILE.gates.router == "allow"
    assert READ_FILE.gates.phase == "allow"


def test_write_file_gates_router_allow_phase_allow():
    """Tier 2: WRITE_FILE has gates.router="allow" and gates.phase="allow"."""
    assert WRITE_FILE.gates.router == "allow"
    assert WRITE_FILE.gates.phase == "allow"


def test_delete_file_gates_router_allow_phase_allow():
    """Tier 2: DELETE_FILE has gates.router="allow" and gates.phase="allow"."""
    assert DELETE_FILE.gates.router == "allow"
    assert DELETE_FILE.gates.phase == "allow"


def test_list_directory_gates_router_allow_phase_allow():
    """Tier 2: LIST_DIRECTORY has gates.router="allow" and gates.phase="allow"."""
    assert LIST_DIRECTORY.gates.router == "allow"
    assert LIST_DIRECTORY.gates.phase == "allow"


# ── 6. Purity invariants ──────────────────────────────────────────────────────

def test_read_file_purity_read_only():
    """Tier 2: READ_FILE purity is 'read_only' — no workspace side effect."""
    assert READ_FILE.purity == "read_only"


def test_list_directory_purity_read_only():
    """Tier 2: LIST_DIRECTORY purity is 'read_only' — no workspace side effect."""
    assert LIST_DIRECTORY.purity == "read_only"


def test_write_file_purity_side_effect():
    """Tier 2: WRITE_FILE purity is 'side_effect' — modifies workspace."""
    assert WRITE_FILE.purity == "side_effect"


def test_delete_file_purity_side_effect():
    """Tier 2: DELETE_FILE purity is 'side_effect' — modifies workspace."""
    assert DELETE_FILE.purity == "side_effect"


# ── 7. Category invariant ─────────────────────────────────────────────────────

def test_all_file_tools_category_io():
    """Tier 2: All four file ToolDefinitions have category='io'."""
    for tool in (READ_FILE, WRITE_FILE, DELETE_FILE, LIST_DIRECTORY):
        assert tool.category == "io", f"{tool.name}.category expected 'io', got {tool.category!r}"


# ── 8. Registry round-trip — all four appear in both for_router and for_phase ─

def test_read_file_appears_in_for_router_and_for_phase():
    """Tier 2: READ_FILE appears in for_router() and for_phase() after registration.
    Guards the allow/allow gate contract for both surfaces."""
    registry = ToolRegistry()
    registry.register(READ_FILE)
    assert READ_FILE in registry.for_router()
    assert READ_FILE in registry.for_phase()


def test_write_file_appears_in_for_router_and_for_phase():
    """Tier 2: WRITE_FILE appears in for_router() and for_phase() after registration."""
    registry = ToolRegistry()
    registry.register(WRITE_FILE)
    assert WRITE_FILE in registry.for_router()
    assert WRITE_FILE in registry.for_phase()


def test_delete_file_appears_in_for_router_and_for_phase():
    """Tier 2: DELETE_FILE appears in for_router() and for_phase() after registration."""
    registry = ToolRegistry()
    registry.register(DELETE_FILE)
    assert DELETE_FILE in registry.for_router()
    assert DELETE_FILE in registry.for_phase()


def test_list_directory_appears_in_for_router_and_for_phase():
    """Tier 2: LIST_DIRECTORY appears in for_router() and for_phase() after registration."""
    registry = ToolRegistry()
    registry.register(LIST_DIRECTORY)
    assert LIST_DIRECTORY in registry.for_router()
    assert LIST_DIRECTORY in registry.for_phase()


# ── 9. Drift detection — module constants match ToolDefinition fields ─────────

def test_read_file_constants_match_definition():
    """Tier 2: _READ_FILE_DESCRIPTION and _READ_FILE_PARAMETERS module constants
    match the READ_FILE ToolDefinition fields. Guards against accidental
    divergence between the constants and what the object holds."""
    assert READ_FILE.description == _READ_FILE_DESCRIPTION
    assert dict(READ_FILE.parameters) == _READ_FILE_PARAMETERS


def test_write_file_constants_match_definition():
    """Tier 2: _WRITE_FILE_DESCRIPTION and _WRITE_FILE_PARAMETERS module constants
    match the WRITE_FILE ToolDefinition fields."""
    assert WRITE_FILE.description == _WRITE_FILE_DESCRIPTION
    assert dict(WRITE_FILE.parameters) == _WRITE_FILE_PARAMETERS


def test_write_edit_descriptions_cross_reference_symmetrically():
    """Tier 2: #187 STEP 1 — file__write and file__edit descriptions reciprocally
    cross-reference each other (general sibling-cross-ref), so the LLM is pointed
    from a whole-file write toward a surgical edit and vice versa. Asserts the
    public surface (concept presence), not exact wording.
    """
    from reyn.tools.file import _EDIT_FILE_DESCRIPTION, _WRITE_FILE_DESCRIPTION

    # write → edit: points to the edit action (by its actionable qualified name)
    # for partial/surgical changes instead of rewriting the whole file.
    write_l = _WRITE_FILE_DESCRIPTION.lower()
    assert "file__edit" in _WRITE_FILE_DESCRIPTION, "write desc must name the edit action"
    assert "edit" in write_l and "whole file" in write_l

    # edit → write: the existing reverse cross-ref (partial edit vs whole-file write).
    edit_l = _EDIT_FILE_DESCRIPTION.lower()
    assert "partial" in edit_l and "whole file" in edit_l


def test_delete_file_constants_match_definition():
    """Tier 2: _DELETE_FILE_DESCRIPTION and _DELETE_FILE_PARAMETERS module constants
    match the DELETE_FILE ToolDefinition fields."""
    assert DELETE_FILE.description == _DELETE_FILE_DESCRIPTION
    assert dict(DELETE_FILE.parameters) == _DELETE_FILE_PARAMETERS


def test_list_directory_constants_match_definition():
    """Tier 2: _LIST_DIRECTORY_DESCRIPTION and _LIST_DIRECTORY_PARAMETERS module
    constants match the LIST_DIRECTORY ToolDefinition fields."""
    assert LIST_DIRECTORY.description == _LIST_DIRECTORY_DESCRIPTION
    assert dict(LIST_DIRECTORY.parameters) == _LIST_DIRECTORY_PARAMETERS
