"""Tier 2: ASK_USER ToolDefinition M3 invariants (ADR-0026 M3 Wave 1).

Verifies that ASK_USER ToolDefinition:
- Has the correct gates: router=deny, phase=allow.
- Has the correct purity and category.
- Is findable via get_default_registry().
- Registers without error and is the single registry entry for ask_user.
- Does NOT appear in registry.for_router() (gates.router=deny).
- DOES appear in registry.for_phase() (gates.phase=allow).
- render_for_phase() produces the correct shape for Control IR context.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools import get_default_registry
from reyn.tools.ask_user import ASK_USER, _ASK_USER_DESCRIPTION, _ASK_USER_PARAMETERS


# ── 1. Gate invariants ────────────────────────────────────────────────────────

def test_ask_user_gates_router_deny():
    """Tier 2: ASK_USER has gates.router=deny (phase-only capability)."""
    assert ASK_USER.gates.router == "deny"


def test_ask_user_gates_phase_allow():
    """Tier 2: ASK_USER has gates.phase=allow."""
    assert ASK_USER.gates.phase == "allow"


# ── 2. Purity and category ────────────────────────────────────────────────────

def test_ask_user_purity_side_effect():
    """Tier 2: ASK_USER purity is 'side_effect' (produces UserIntervention)."""
    assert ASK_USER.purity == "side_effect"


def test_ask_user_category_interactive():
    """Tier 2: ASK_USER category is 'interactive'."""
    assert ASK_USER.category == "interactive"


# ── 3. Identity ───────────────────────────────────────────────────────────────

def test_ask_user_name():
    """Tier 2: ASK_USER name is 'ask_user'."""
    assert ASK_USER.name == "ask_user"


def test_ask_user_description_constant_matches_definition():
    """Tier 2: _ASK_USER_DESCRIPTION module constant matches ASK_USER.description.
    Ensures no accidental divergence between the constant and what ASK_USER holds."""
    assert ASK_USER.description == _ASK_USER_DESCRIPTION


def test_ask_user_parameters_constant_matches_definition():
    """Tier 2: _ASK_USER_PARAMETERS module constant matches ASK_USER.parameters.
    Ensures no accidental divergence."""
    assert dict(ASK_USER.parameters) == _ASK_USER_PARAMETERS


# ── 4. Parameters schema shape ────────────────────────────────────────────────

def test_ask_user_parameters_required_field():
    """Tier 2: ASK_USER parameters schema requires 'question'."""
    assert _ASK_USER_PARAMETERS["required"] == ["question"]


def test_ask_user_parameters_question_is_string():
    """Tier 2: ASK_USER parameters schema has question as string type."""
    assert _ASK_USER_PARAMETERS["properties"]["question"] == {"type": "string"}


def test_ask_user_parameters_suggestions_is_array():
    """Tier 2: ASK_USER parameters schema has suggestions as array of strings."""
    suggestions = _ASK_USER_PARAMETERS["properties"]["suggestions"]
    assert suggestions["type"] == "array"
    assert suggestions["items"] == {"type": "string"}


def test_ask_user_parameters_required_is_boolean():
    """Tier 2: ASK_USER parameters schema has required flag as boolean type."""
    assert _ASK_USER_PARAMETERS["properties"]["required"] == {"type": "boolean"}


# ── 5. Registry lookup ────────────────────────────────────────────────────────

def test_default_registry_contains_ask_user():
    """Tier 2: get_default_registry() returns a registry that contains ask_user."""
    registry = get_default_registry()
    assert "ask_user" in registry


def test_default_registry_lookup_returns_ask_user_instance():
    """Tier 2: registry.lookup('ask_user') returns the ASK_USER instance."""
    registry = get_default_registry()
    found = registry.lookup("ask_user")
    assert found is ASK_USER


def test_default_registry_ask_user_not_in_for_router():
    """Tier 2: ASK_USER does NOT appear in registry.for_router() (gates.router=deny)."""
    registry = get_default_registry()
    router_tools = registry.for_router()
    assert ASK_USER not in router_tools


def test_default_registry_ask_user_in_for_phase():
    """Tier 2: ASK_USER appears in registry.for_phase() (gates.phase=allow)."""
    registry = get_default_registry()
    phase_tools = registry.for_phase()
    assert ASK_USER in phase_tools


# ── 6. render_for_phase shape ─────────────────────────────────────────────────

def test_ask_user_render_for_phase_shape():
    """Tier 2: ASK_USER.render_for_phase() produces the correct Control IR shape."""
    rendered = ASK_USER.render_for_phase()

    assert rendered["kind"] == "ask_user"
    assert "description" in rendered
    assert rendered["purity"] == "side_effect"
    assert "args_schema" in rendered
    assert rendered["args_schema"]["type"] == "object"
    assert "question" in rendered["args_schema"]["properties"]


def test_ask_user_render_for_phase_kind_matches_name():
    """Tier 2: render_for_phase() kind equals ASK_USER.name."""
    rendered = ASK_USER.render_for_phase()
    assert rendered["kind"] == ASK_USER.name


# ── 7. Drift detection — description and parameters match render ──────────────

def test_ask_user_render_for_phase_args_schema_matches_parameters():
    """Tier 2: render_for_phase() args_schema matches _ASK_USER_PARAMETERS."""
    rendered = ASK_USER.render_for_phase()
    assert rendered["args_schema"] == _ASK_USER_PARAMETERS
