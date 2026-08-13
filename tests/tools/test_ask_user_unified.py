"""Tier 2: ASK_USER ToolDefinition M3 invariants (ADR-0026 M3 Wave 1).

Verifies that ASK_USER ToolDefinition:
- Has the correct gates: router=deny.
- Has the correct purity and category.
- Is findable via get_default_registry().
- Registers without error and is the single registry entry for ask_user.
- Does NOT appear in registry.for_router() (gates.router=deny).

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

from reyn.tools import get_default_registry
from reyn.tools.ask_user import _ASK_USER_DESCRIPTION, _ASK_USER_PARAMETERS, ASK_USER

# ── 1. Gate invariants ────────────────────────────────────────────────────────

def test_ask_user_gates_router_deny():
    """Tier 2: ASK_USER has gates.router=deny (not advertised to the router)."""
    assert ASK_USER.gates.router == "deny"


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


# ── 7. Drift detection — description and parameters match render ──────────────

