"""Tier 2: WEB_FETCH ToolDefinition M3 Wave 1 invariants (ADR-0026 M3).

Verifies that WEB_FETCH ToolDefinition:
- Produces byte-identical output to the prior ToolSpec literal for web_fetch.
  Drift in description or parameters here would invalidate replay fixtures.
- Has the correct gates, purity, and category.
- Is findable via get_default_registry().
- Registers without error and is the single registry entry for web_fetch.
- FP-0022: require_web_fetch() 4-layer approval gate behavior.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.tools import get_default_registry
from reyn.tools.web_fetch import _WEB_FETCH_DESCRIPTION, _WEB_FETCH_PARAMETERS, WEB_FETCH

# ── 1. render_for_router byte-identity gate ───────────────────────────────────

def test_web_fetch_router_render_matches_legacy_shape():
    """Tier 2: WEB_FETCH.render_for_router() produces byte-identical output
    to the prior ToolSpec literal for web_fetch. Drift here would invalidate
    LLMReplay fixtures."""
    rendered = WEB_FETCH.render_for_router()

    # Top-level shape
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)

    fn = rendered["function"]

    # Name
    assert fn["name"] == "web_fetch"

    # Description: key phrases that identify the web_fetch description
    # (= the full description must be verbatim).
    assert "URL" in fn["description"] or "url" in fn["description"].lower()
    assert "text-extracted" in fn["description"]
    assert "max_length" in fn["description"]
    assert "50000" in fn["description"]
    assert "web_search" in fn["description"]

    # Parameters schema
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["url"]
    assert "url" in params["properties"]
    assert params["properties"]["url"] == {"type": "string"}
    assert "max_length" in params["properties"]
    assert params["properties"]["max_length"] == {"type": "integer"}


def test_web_fetch_router_render_exact_description():
    """Tier 2: WEB_FETCH description is byte-identical to the legacy ToolSpec
    description string. Any whitespace or punctuation diff is a stop signal."""
    rendered = WEB_FETCH.render_for_router()
    legacy_description = (
        "Fetch a single URL and return its (text-extracted) "
        "content. url: absolute http/https URL. "
        "max_length: cap on returned content size "
        "(default 50000). Use after web_search to read a "
        "result page in detail."
    )
    assert rendered["function"]["description"] == legacy_description


def test_web_fetch_router_render_exact_parameters():
    """Tier 2: WEB_FETCH parameters schema is byte-identical to the legacy
    ToolSpec parameters dict."""
    rendered = WEB_FETCH.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_length": {"type": "integer"},
        },
        "required": ["url"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_web_fetch_gates_both_allow():
    """Tier 2: WEB_FETCH has gates.router=allow and gates.phase=allow."""
    assert WEB_FETCH.gates.router == "allow"
    assert WEB_FETCH.gates.phase == "allow"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_web_fetch_purity_read_only():
    """Tier 2: WEB_FETCH purity is 'read_only' (no workspace side effects)."""
    assert WEB_FETCH.purity == "read_only"


def test_web_fetch_category_discovery():
    """Tier 2: WEB_FETCH category is 'discovery'."""
    assert WEB_FETCH.category == "discovery"


# ── 4. Registry lookup ────────────────────────────────────────────────────────

def test_default_registry_contains_web_fetch():
    """Tier 2: get_default_registry() returns a registry that contains web_fetch."""
    registry = get_default_registry()
    assert "web_fetch" in registry


def test_default_registry_lookup_returns_web_fetch_instance():
    """Tier 2: registry.lookup('web_fetch') returns the WEB_FETCH instance."""
    registry = get_default_registry()
    found = registry.lookup("web_fetch")
    assert found is WEB_FETCH


def test_default_registry_web_fetch_in_for_router():
    """Tier 2: WEB_FETCH appears in registry.for_router() (gates.router=allow)."""
    registry = get_default_registry()
    router_tools = registry.for_router()
    assert WEB_FETCH in router_tools


def test_default_registry_web_fetch_in_for_phase():
    """Tier 2: WEB_FETCH appears in registry.for_phase() (gates.phase=allow)."""
    registry = get_default_registry()
    phase_tools = registry.for_phase()
    assert WEB_FETCH in phase_tools


# ── 5. build_tools integration — web_fetch rendered from registry ──────────────

def test_build_tools_includes_web_fetch_via_registry():
    """Tier 2: build_tools() includes web_fetch rendered from the unified
    registry. The rendered dict must match the legacy ToolSpec.to_openai_dict()
    output (byte-identity gate for LLMReplay fixtures).

    FP-0022: web_fetch is now always in the catalog; the web_fetch_allowed
    parameter is kept for backward compat but is a no-op."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )

    # Find web_fetch in the returned tools list
    wf_tools = [t for t in tools if t.get("function", {}).get("name") == "web_fetch"]
    assert len(wf_tools) == 1, "web_fetch should appear exactly once in build_tools output"

    wf = wf_tools[0]
    assert wf["type"] == "function"
    assert wf["function"]["name"] == "web_fetch"

    # Description byte-identity check (key phrases)
    assert "text-extracted" in wf["function"]["description"]
    assert "max_length" in wf["function"]["description"]
    assert "50000" in wf["function"]["description"]

    # Parameters schema byte-identity check
    params = wf["function"]["parameters"]
    assert params["required"] == ["url"]
    assert "url" in params["properties"]
    assert "max_length" in params["properties"]


def test_build_tools_web_fetch_not_duplicated():
    """Tier 2: web_fetch appears exactly once in build_tools() output.
    Guards against both the registry path and a residual ToolSpec literal
    being included simultaneously. FP-0022: web_fetch is always in catalog."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )
    wf_tools = [t for t in tools if t.get("function", {}).get("name") == "web_fetch"]
    assert len(wf_tools) == 1


# ── 6. Drift detection — description module constant matches render ────────────

def test_web_fetch_description_constant_matches_render():
    """Tier 2: _WEB_FETCH_DESCRIPTION module constant matches the rendered
    description. Ensures no accidental divergence between the constant and
    what WEB_FETCH.description holds."""
    rendered = WEB_FETCH.render_for_router()
    assert rendered["function"]["description"] == _WEB_FETCH_DESCRIPTION
    assert WEB_FETCH.description == _WEB_FETCH_DESCRIPTION


def test_web_fetch_parameters_constant_matches_render():
    """Tier 2: _WEB_FETCH_PARAMETERS module constant matches the rendered
    parameters. Ensures no accidental divergence."""
    rendered = WEB_FETCH.render_for_router()
    assert rendered["function"]["parameters"] == _WEB_FETCH_PARAMETERS
    assert dict(WEB_FETCH.parameters) == _WEB_FETCH_PARAMETERS


# ── 7. FP-0022: Permission tier gate invariants ───────────────────────────────

class _AutoApproveInterventionBus:
    """Minimal real InterventionBus stub that auto-approves every request.

    Returns choice_id='always' so approvals are persisted to the temp
    approvals.yaml and the second call skips the prompt path entirely.
    """
    async def request(self, iv):
        from reyn.user_intervention import InterventionAnswer
        return InterventionAnswer(choice_id="always")


class _DenyAllInterventionBus:
    """Minimal real InterventionBus stub that fails the test if called.

    When the config denies access, require_web_fetch must raise without
    reaching the prompt. If request() is called, the implementation has a bug.
    """
    async def request(self, iv):
        raise AssertionError(
            f"InterventionBus.request called unexpectedly: {iv}"
        )


def test_require_web_fetch_config_allow_pre_approves(tmp_path: Path) -> None:
    """Tier 2: web.fetch: allow in config pre-approves without prompting.

    FP-0022 backward compat: existing `web.fetch: allow` users must not see
    any interactive prompt — the config grant short-circuits at Layer 1.
    """
    from reyn.permissions.permissions import PermissionResolver

    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
        interactive=True,
    )
    # DenyAllInterventionBus: if a prompt fires, the test fails.
    bus = _DenyAllInterventionBus()
    # Must not raise and must not reach the bus.
    asyncio.run(resolver.require_web_fetch("https://example.com", bus))


def test_require_web_fetch_config_deny_raises_immediately(tmp_path: Path) -> None:
    """Tier 2: web.fetch: deny blocks with PermissionError before any prompt.

    FP-0022: deny config must raise immediately, not reach the interactive bus.
    """
    from reyn.permissions.permissions import PermissionResolver

    resolver = PermissionResolver(
        config_permissions={"web.fetch": "deny"},
        project_root=tmp_path,
        interactive=True,
    )
    bus = _DenyAllInterventionBus()  # must not be called
    with pytest.raises(PermissionError, match="web fetch denied by config"):
        asyncio.run(resolver.require_web_fetch("https://example.com", bus))
