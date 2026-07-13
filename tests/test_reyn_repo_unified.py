"""Tier 2: REYN_REPO_LIST / REYN_REPO_READ ToolDefinition M3 invariants (ADR-0026 M3 Wave 1).

Verifies that REYN_REPO_LIST and REYN_REPO_READ ToolDefinitions:
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

from reyn.tools.registry import ToolRegistry
from reyn.tools.reyn_repo import (
    _REYN_REPO_LIST_DESCRIPTION,
    _REYN_REPO_LIST_PARAMETERS,
    _REYN_REPO_READ_DESCRIPTION,
    _REYN_REPO_READ_PARAMETERS,
    REYN_REPO_LIST,
    REYN_REPO_READ,
)

# ── 1. REYN_REPO_LIST render_for_router byte-identity ────────────────────────

def test_reyn_repo_list_router_render_exact_description():
    """Tier 2: render_for_router() passes the ToolDefinition's own description
    through unchanged (no router-side transformation/truncation).

    Asserts against the imported ``_REYN_REPO_LIST_DESCRIPTION`` constant
    (the single source of truth in reyn_repo.py) rather than a second,
    independently-typed literal copy — a duplicated-literal pin is exactly
    what let the description drift stale in the first place (it referenced
    a "docs/en/concepts" path from before the docs i18n restructure,
    reliably steering agents into guessing nonexistent paths, caught via a
    real dogfood-journal grep sweep). One string, one place to update."""
    rendered = REYN_REPO_LIST.render_for_router()
    assert rendered["function"]["description"] == _REYN_REPO_LIST_DESCRIPTION


def test_reyn_repo_list_router_render_exact_parameters():
    """Tier 2: REYN_REPO_LIST parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py."""
    rendered = REYN_REPO_LIST.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. REYN_REPO_READ render_for_router byte-identity ────────────────────────

def test_reyn_repo_read_router_render_exact_description():
    """Tier 2: REYN_REPO_READ description is byte-identical to the canonical text.

    Originally pinned the legacy text against ADR-0026 migration drift.
    Updated in B22 (= 2026-05-10 schema-layer fix for affordance-bias
    attractor observed in batch 21). The previous claim "Use this for
    any 'how does Reyn / how does Reyn's X work?' question" pulled the
    LLM to file_read with hallucinated paths even when an indexed
    source covered the topic. New text follows the practitioner 4-part
    template (= what / when / when NOT / cross-reference to recall),
    preserves the README curated-navigation fallback (= constraint C2
    from the description history audit), and preserves the no-web-
    search directive (= original HN first-touch motivation).
    """
    rendered = REYN_REPO_READ.render_for_router()
    canonical_description = (
        "Read a text file from Reyn's own repository by an exact "
        "repo-root-relative path. Use for: (a) reading a specific file the "
        "user named (e.g. README.md), or (b) navigating "
        "Reyn's source / docs when NO indexed source covers the topic. "
        "If an indexed source description mentions concepts / design / "
        "docs / Reyn, use `semantic_search` instead — guessing a file path is "
        "unreliable; semantic search over indexed chunks is not. Fallback "
        "entry point: reyn_repo_read(\"README.md\") for the overview + "
        "curated map of deep-dive paths."
    )
    assert rendered["function"]["description"] == canonical_description


def test_reyn_repo_read_router_render_exact_parameters():
    """Tier 2: REYN_REPO_READ parameters schema mirrors ``read_file`` /
    ``read_memory_body`` — required ``path`` plus optional ``offset`` /
    ``limit`` line-slice args. The slice path additionally bypasses the
    256-KB byte cap (= line-streaming the requested range only)."""
    rendered = REYN_REPO_READ.render_for_router()
    expected_parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {
                "type": "integer",
                "description": (
                    "Line number to start reading from (0-indexed). "
                    "Omit to start at the beginning of the file. When set "
                    "(with or without limit), the 256-KB byte cap is "
                    "bypassed by line-streaming only the requested slice."
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


# ── 3. Gate invariants ────────────────────────────────────────────────────────

def test_reyn_repo_list_gates_router_allow_phase_deny():
    """Tier 2: REYN_REPO_LIST has gates.router="allow" and gates.phase="deny".
    Phase doesn't need dev-debug tools; this is an operator-side capability."""
    assert REYN_REPO_LIST.gates.router == "allow"
    assert REYN_REPO_LIST.gates.phase == "deny"


def test_reyn_repo_read_gates_router_allow_phase_deny():
    """Tier 2: REYN_REPO_READ has gates.router="allow" and gates.phase="deny".
    Phase doesn't need dev-debug tools; this is an operator-side capability."""
    assert REYN_REPO_READ.gates.router == "allow"
    assert REYN_REPO_READ.gates.phase == "deny"


# ── 4. Purity and category ────────────────────────────────────────────────────

def test_reyn_repo_list_purity_and_category():
    """Tier 2: REYN_REPO_LIST purity is 'read_only' and category is 'dev'."""
    assert REYN_REPO_LIST.purity == "read_only"
    assert REYN_REPO_LIST.category == "dev"


def test_reyn_repo_read_purity_and_category():
    """Tier 2: REYN_REPO_READ purity is 'read_only' and category is 'dev'."""
    assert REYN_REPO_READ.purity == "read_only"
    assert REYN_REPO_READ.category == "dev"


# ── 5. Registry gate filtering ────────────────────────────────────────────────

def test_reyn_repo_list_appears_in_for_router_not_for_phase():
    """Tier 2: REYN_REPO_LIST appears in for_router() but not for_phase().
    Guards the router-only gate contract."""
    registry = ToolRegistry()
    registry.register(REYN_REPO_LIST)
    assert REYN_REPO_LIST in registry.for_router()
    assert REYN_REPO_LIST not in registry.for_phase()


def test_reyn_repo_read_appears_in_for_router_not_for_phase():
    """Tier 2: REYN_REPO_READ appears in for_router() but not for_phase().
    Guards the router-only gate contract."""
    registry = ToolRegistry()
    registry.register(REYN_REPO_READ)
    assert REYN_REPO_READ in registry.for_router()
    assert REYN_REPO_READ not in registry.for_phase()


# ── 6. Drift detection — description/parameters module constants match render ─

def test_reyn_repo_list_constants_match_definition():
    """Tier 2: _REYN_REPO_LIST_DESCRIPTION and _REYN_REPO_LIST_PARAMETERS module
    constants match the REYN_REPO_LIST ToolDefinition fields. Guards against
    accidental divergence between the constants and what the object holds."""
    assert REYN_REPO_LIST.description == _REYN_REPO_LIST_DESCRIPTION
    assert dict(REYN_REPO_LIST.parameters) == _REYN_REPO_LIST_PARAMETERS


def test_reyn_repo_read_constants_match_definition():
    """Tier 2: _REYN_REPO_READ_DESCRIPTION and _REYN_REPO_READ_PARAMETERS module
    constants match the REYN_REPO_READ ToolDefinition fields. Guards against
    accidental divergence between the constants and what the object holds."""
    assert REYN_REPO_READ.description == _REYN_REPO_READ_DESCRIPTION
    assert dict(REYN_REPO_READ.parameters) == _REYN_REPO_READ_PARAMETERS
