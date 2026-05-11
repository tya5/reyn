"""Tier 2: WEB_SEARCH ToolDefinition M2 invariants (ADR-0026 M2).

Verifies that WEB_SEARCH ToolDefinition:
- Produces byte-identical output to the prior ToolSpec literal for web_search.
  Drift in description or parameters here would invalidate replay fixtures.
- Has the correct gates, purity, and category.
- Is findable via get_default_registry().
- Registers without error and is the single registry entry for web_search.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools import get_default_registry
from reyn.tools.web_search import _WEB_SEARCH_DESCRIPTION, _WEB_SEARCH_PARAMETERS, WEB_SEARCH

# ── 1. render_for_router byte-identity gate ───────────────────────────────────

def test_web_search_router_render_matches_legacy_shape():
    """Tier 2: WEB_SEARCH.render_for_router() produces byte-identical output
    to the prior ToolSpec literal for web_search. Drift here would invalidate
    LLMReplay fixtures."""
    rendered = WEB_SEARCH.render_for_router()

    # Top-level shape
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)

    fn = rendered["function"]

    # Name
    assert fn["name"] == "web_search"

    # Description: key phrases that identify the extended operator-hint
    # string from commit 8af3444 (= the full description must be verbatim).
    assert "DuckDuckGo" in fn["description"]
    assert "site:news.ycombinator.com" in fn["description"]
    assert "phrase" in fn["description"]
    assert "-term" in fn["description"]
    assert "query: search string." in fn["description"]
    assert "max_results: cap on returned results (default 5)." in fn["description"]

    # Parameters schema
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["query"]
    assert "query" in params["properties"]
    assert params["properties"]["query"] == {"type": "string"}
    assert "max_results" in params["properties"]
    assert params["properties"]["max_results"] == {"type": "integer"}


def test_web_search_router_render_exact_description():
    """Tier 2: WEB_SEARCH description is byte-identical to the legacy ToolSpec
    description string. Any whitespace or punctuation diff is a stop signal."""
    rendered = WEB_SEARCH.render_for_router()
    legacy_description = (
        "Search the public web with DuckDuckGo and return "
        "structured results. Standard search operators are "
        "supported in `query`: `site:<domain>` to scope to "
        "one site (e.g. `site:news.ycombinator.com`), "
        "`\"phrase\"` for exact match, `-term` to exclude. "
        "Use them when the user's intent is site-specific "
        "or phrase-anchored; plain keywords work otherwise. "
        "query: search string. "
        "max_results: cap on returned results (default 5)."
    )
    assert rendered["function"]["description"] == legacy_description


def test_web_search_router_render_exact_parameters():
    """Tier 2: WEB_SEARCH parameters schema is byte-identical to the legacy
    ToolSpec parameters dict."""
    rendered = WEB_SEARCH.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_web_search_gates_both_allow():
    """Tier 2: WEB_SEARCH has gates.router=allow and gates.phase=allow."""
    assert WEB_SEARCH.gates.router == "allow"
    assert WEB_SEARCH.gates.phase == "allow"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_web_search_purity_read_only():
    """Tier 2: WEB_SEARCH purity is 'read_only' (no workspace side effects)."""
    assert WEB_SEARCH.purity == "read_only"


def test_web_search_category_discovery():
    """Tier 2: WEB_SEARCH category is 'discovery'."""
    assert WEB_SEARCH.category == "discovery"


# ── 4. Registry lookup ────────────────────────────────────────────────────────

def test_default_registry_contains_web_search():
    """Tier 2: get_default_registry() returns a registry that contains web_search."""
    registry = get_default_registry()
    assert "web_search" in registry


def test_default_registry_lookup_returns_web_search_instance():
    """Tier 2: registry.lookup('web_search') returns the WEB_SEARCH instance."""
    registry = get_default_registry()
    found = registry.lookup("web_search")
    assert found is WEB_SEARCH


def test_default_registry_web_search_in_for_router():
    """Tier 2: WEB_SEARCH appears in registry.for_router() (gates.router=allow)."""
    registry = get_default_registry()
    router_tools = registry.for_router()
    assert WEB_SEARCH in router_tools


def test_default_registry_web_search_in_for_phase():
    """Tier 2: WEB_SEARCH appears in registry.for_phase() (gates.phase=allow)."""
    registry = get_default_registry()
    phase_tools = registry.for_phase()
    assert WEB_SEARCH in phase_tools


# ── 5. build_tools integration — web_search rendered from registry ─────────────

def test_build_tools_includes_web_search_via_registry():
    """Tier 2: build_tools() includes web_search rendered from the unified
    registry. The rendered dict must match the legacy ToolSpec.to_openai_dict()
    output (byte-identity gate for LLMReplay fixtures)."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )

    # Find web_search in the returned tools list
    ws_tools = [t for t in tools if t.get("function", {}).get("name") == "web_search"]
    assert len(ws_tools) == 1, "web_search should appear exactly once in build_tools output"

    ws = ws_tools[0]
    assert ws["type"] == "function"
    assert ws["function"]["name"] == "web_search"

    # Description byte-identity check (key phrases)
    assert "DuckDuckGo" in ws["function"]["description"]
    assert "site:news.ycombinator.com" in ws["function"]["description"]
    assert "max_results: cap on returned results (default 5)." in ws["function"]["description"]

    # Parameters schema byte-identity check
    params = ws["function"]["parameters"]
    assert params["required"] == ["query"]
    assert "query" in params["properties"]
    assert "max_results" in params["properties"]


def test_build_tools_web_search_not_duplicated():
    """Tier 2: web_search appears exactly once in build_tools() output.
    Guards against both the registry path and a residual ToolSpec literal
    being included simultaneously."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )
    ws_tools = [t for t in tools if t.get("function", {}).get("name") == "web_search"]
    assert len(ws_tools) == 1


# ── 6. Drift detection — description module constant matches render ────────────

def test_web_search_description_constant_matches_render():
    """Tier 2: _WEB_SEARCH_DESCRIPTION module constant matches the rendered
    description. Ensures no accidental divergence between the constant and
    what WEB_SEARCH.description holds."""
    rendered = WEB_SEARCH.render_for_router()
    assert rendered["function"]["description"] == _WEB_SEARCH_DESCRIPTION
    assert WEB_SEARCH.description == _WEB_SEARCH_DESCRIPTION


def test_web_search_parameters_constant_matches_render():
    """Tier 2: _WEB_SEARCH_PARAMETERS module constant matches the rendered
    parameters. Ensures no accidental divergence."""
    rendered = WEB_SEARCH.render_for_router()
    assert rendered["function"]["parameters"] == _WEB_SEARCH_PARAMETERS
    assert dict(WEB_SEARCH.parameters) == _WEB_SEARCH_PARAMETERS
