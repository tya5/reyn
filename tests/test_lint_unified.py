"""Tier 2: LINT ToolDefinition M3 Wave 1 invariants (ADR-0026 M3).

Verifies that LINT ToolDefinition:
- Produces correct output from render_for_router().
- Has the correct gates (router=allow, phase=allow), purity, and category.
- Registers without error and is the single registry entry for lint.
- Is included in both for_router() and for_phase().

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

from reyn.tools.lint import _LINT_DESCRIPTION, _LINT_PARAMETERS, LINT
from reyn.tools.registry import ToolRegistry

# ── 1. render_for_router shape gate ───────────────────────────────────────────

def test_lint_router_render_matches_shape():
    """Tier 2: LINT.render_for_router() produces a correctly-shaped dict."""
    rendered = LINT.render_for_router()

    # Top-level shape
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)

    fn = rendered["function"]

    # Name
    assert fn["name"] == "lint"

    # Description: key phrases
    assert "skill_path" in fn["description"]
    assert "linter" in fn["description"] or "lint" in fn["description"].lower()

    # Parameters schema
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["skill_path"]
    assert "skill_path" in params["properties"]
    assert params["properties"]["skill_path"] == {"type": "string"}


def test_lint_router_render_exact_description():
    """Tier 2: LINT description is byte-identical to the _LINT_DESCRIPTION
    constant. Any whitespace or punctuation diff is a stop signal."""
    rendered = LINT.render_for_router()
    assert rendered["function"]["description"] == _LINT_DESCRIPTION


def test_lint_router_render_exact_parameters():
    """Tier 2: LINT parameters schema is byte-identical to _LINT_PARAMETERS."""
    rendered = LINT.render_for_router()
    assert rendered["function"]["parameters"] == _LINT_PARAMETERS


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_lint_gates_router_allow():
    """Tier 2: LINT has gates.router=allow (router-accessible via validation__lint)."""
    assert LINT.gates.router == "allow"


def test_lint_gates_phase_allow():
    """Tier 2: LINT has gates.phase=allow."""
    assert LINT.gates.phase == "allow"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_lint_purity_read_only():
    """Tier 2: LINT purity is 'read_only' (lint reads + reports, no mutation)."""
    assert LINT.purity == "read_only"


def test_lint_category_validation():
    """Tier 2: LINT category is 'validation'."""
    assert LINT.category == "validation"


# ── 4. Registry invariants ────────────────────────────────────────────────────

def test_registry_contains_lint_after_register():
    """Tier 2: A ToolRegistry contains lint after LINT is registered."""
    registry = ToolRegistry()
    registry.register(LINT)
    assert "lint" in registry


def test_registry_lookup_returns_lint_instance():
    """Tier 2: registry.lookup('lint') returns the LINT instance."""
    registry = ToolRegistry()
    registry.register(LINT)
    found = registry.lookup("lint")
    assert found is LINT


def test_registry_lint_in_for_router():
    """Tier 2: LINT appears in registry.for_router() (gates.router=allow)."""
    registry = ToolRegistry()
    registry.register(LINT)
    router_tools = registry.for_router()
    assert LINT in router_tools


def test_registry_lint_in_for_phase():
    """Tier 2: LINT appears in registry.for_phase() (gates.phase=allow)."""
    registry = ToolRegistry()
    registry.register(LINT)
    phase_tools = registry.for_phase()
    assert LINT in phase_tools


# ── 5. Drift detection — module constants match render ────────────────────────

def test_lint_description_constant_matches_render():
    """Tier 2: _LINT_DESCRIPTION module constant matches the rendered
    description. Ensures no accidental divergence."""
    rendered = LINT.render_for_router()
    assert rendered["function"]["description"] == _LINT_DESCRIPTION
    assert LINT.description == _LINT_DESCRIPTION


def test_lint_parameters_constant_matches_render():
    """Tier 2: _LINT_PARAMETERS module constant matches the rendered
    parameters. Ensures no accidental divergence."""
    rendered = LINT.render_for_router()
    assert rendered["function"]["parameters"] == _LINT_PARAMETERS
    assert dict(LINT.parameters) == _LINT_PARAMETERS
