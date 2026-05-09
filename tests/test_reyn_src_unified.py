"""Tier 2: REYN_SRC_LIST / REYN_SRC_READ ToolDefinition M3 invariants (ADR-0026 M3 Wave 1).

Verifies that REYN_SRC_LIST and REYN_SRC_READ ToolDefinitions:
- Produce byte-identical description/parameters output to the prior ToolSpec
  literals in router_tools.py. Drift would invalidate replay fixtures.
- Have gates.router="allow" and gates.phase="deny" (router-only dev tools).
- Have purity="read_only" and category="dev".
- Are findable via the default registry after registration.

No mocks of collaborators. All tests use real ToolDefinition instances.
No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.reyn_src import (
    REYN_SRC_LIST,
    REYN_SRC_READ,
    _REYN_SRC_LIST_DESCRIPTION,
    _REYN_SRC_LIST_PARAMETERS,
    _REYN_SRC_READ_DESCRIPTION,
    _REYN_SRC_READ_PARAMETERS,
)
from reyn.tools.registry import ToolRegistry


# ── 1. REYN_SRC_LIST render_for_router byte-identity ────────────────────────

def test_reyn_src_list_router_render_exact_description():
    """Tier 2: REYN_SRC_LIST description is byte-identical to the legacy ToolSpec
    description in router_tools.py. Any whitespace or punctuation diff is a stop
    signal that would drift LLMReplay fixtures."""
    rendered = REYN_SRC_LIST.render_for_router()
    legacy_description = (
        "List entries under a path inside Reyn's own repository "
        "(= the project that built this agent). Pass \"\" for "
        "the repo root. Returns names + types (file/dir). Use "
        "this to discover Reyn's source/doc layout before "
        "reading specific files. Examples: list \"\" for the "
        "top-level layout, \"docs/en/concepts\" for concept "
        "docs, \"src/reyn/chat\" for the chat layer source."
    )
    assert rendered["function"]["description"] == legacy_description


def test_reyn_src_list_router_render_exact_parameters():
    """Tier 2: REYN_SRC_LIST parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py."""
    rendered = REYN_SRC_LIST.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. REYN_SRC_READ render_for_router byte-identity ────────────────────────

def test_reyn_src_read_router_render_exact_description():
    """Tier 2: REYN_SRC_READ description is byte-identical to the legacy ToolSpec
    description in router_tools.py. Any whitespace or punctuation diff is a stop
    signal that would drift LLMReplay fixtures."""
    rendered = REYN_SRC_READ.render_for_router()
    legacy_description = (
        "Read a text file from Reyn's own repository. Path is "
        "repo-root-relative (= same paths the user sees on "
        "GitHub). Start with reyn_src_read(\"README.md\") for "
        "an overview and a curated index of deep-dive paths. "
        "Use this for any \"how does Reyn / how does Reyn's X "
        "work?\" question — Reyn's source is the authoritative "
        "answer, not web search."
    )
    assert rendered["function"]["description"] == legacy_description


def test_reyn_src_read_router_render_exact_parameters():
    """Tier 2: REYN_SRC_READ parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py."""
    rendered = REYN_SRC_READ.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 3. Gate invariants ────────────────────────────────────────────────────────

def test_reyn_src_list_gates_router_allow_phase_deny():
    """Tier 2: REYN_SRC_LIST has gates.router="allow" and gates.phase="deny".
    Phase doesn't need dev-debug tools; this is an operator-side capability."""
    assert REYN_SRC_LIST.gates.router == "allow"
    assert REYN_SRC_LIST.gates.phase == "deny"


def test_reyn_src_read_gates_router_allow_phase_deny():
    """Tier 2: REYN_SRC_READ has gates.router="allow" and gates.phase="deny".
    Phase doesn't need dev-debug tools; this is an operator-side capability."""
    assert REYN_SRC_READ.gates.router == "allow"
    assert REYN_SRC_READ.gates.phase == "deny"


# ── 4. Purity and category ────────────────────────────────────────────────────

def test_reyn_src_list_purity_and_category():
    """Tier 2: REYN_SRC_LIST purity is 'read_only' and category is 'dev'."""
    assert REYN_SRC_LIST.purity == "read_only"
    assert REYN_SRC_LIST.category == "dev"


def test_reyn_src_read_purity_and_category():
    """Tier 2: REYN_SRC_READ purity is 'read_only' and category is 'dev'."""
    assert REYN_SRC_READ.purity == "read_only"
    assert REYN_SRC_READ.category == "dev"


# ── 5. Registry gate filtering ────────────────────────────────────────────────

def test_reyn_src_list_appears_in_for_router_not_for_phase():
    """Tier 2: REYN_SRC_LIST appears in for_router() but not for_phase().
    Guards the router-only gate contract."""
    registry = ToolRegistry()
    registry.register(REYN_SRC_LIST)
    assert REYN_SRC_LIST in registry.for_router()
    assert REYN_SRC_LIST not in registry.for_phase()


def test_reyn_src_read_appears_in_for_router_not_for_phase():
    """Tier 2: REYN_SRC_READ appears in for_router() but not for_phase().
    Guards the router-only gate contract."""
    registry = ToolRegistry()
    registry.register(REYN_SRC_READ)
    assert REYN_SRC_READ in registry.for_router()
    assert REYN_SRC_READ not in registry.for_phase()


# ── 6. Drift detection — description/parameters module constants match render ─

def test_reyn_src_list_constants_match_definition():
    """Tier 2: _REYN_SRC_LIST_DESCRIPTION and _REYN_SRC_LIST_PARAMETERS module
    constants match the REYN_SRC_LIST ToolDefinition fields. Guards against
    accidental divergence between the constants and what the object holds."""
    assert REYN_SRC_LIST.description == _REYN_SRC_LIST_DESCRIPTION
    assert dict(REYN_SRC_LIST.parameters) == _REYN_SRC_LIST_PARAMETERS


def test_reyn_src_read_constants_match_definition():
    """Tier 2: _REYN_SRC_READ_DESCRIPTION and _REYN_SRC_READ_PARAMETERS module
    constants match the REYN_SRC_READ ToolDefinition fields. Guards against
    accidental divergence between the constants and what the object holds."""
    assert REYN_SRC_READ.description == _REYN_SRC_READ_DESCRIPTION
    assert dict(REYN_SRC_READ.parameters) == _REYN_SRC_READ_PARAMETERS
