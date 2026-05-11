"""Tier 2: INVOKE_SKILL ToolDefinition M3 Wave 2 invariants (ADR-0026 M3).

Verifies that INVOKE_SKILL ToolDefinition:
- Has the canonical name "invoke_skill" (= ADR-0026 Open Q #6 resolution:
  router-side name adopted as canonical; phase-side run_skill is a
  backward-compat alias via OP_KIND_MODEL_MAP, deferred to M4).
- Has gates router=allow, phase=allow (= both surfaces may invoke it).
- Carries the byte-identical description from router_tools.py invoke_skill
  ToolSpec (= excluding dynamic enum content, which is a per-call
  router-side enrichment not handled by render_for_router()).
- Has the correct static base parameters shape (= without the per-call
  dynamic enum on the 'name' field).
- Purity and category are correctly declared.
- Is findable via get_default_registry().

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.

Per-call enum injection note (documented here as a Tier 2 invariant of
expected absence): render_for_router() on this ToolDefinition does NOT
inject the runtime skill-name enum into the 'name' field. That enrichment
is a router-side concern (= _invoke_skill_name_schema in router_tools.py)
not handled by the registry render. M4 may introduce a per-call render
override mechanism; until then, the router inline logic remains canonical
for enum injection.
"""
from __future__ import annotations

import pytest

from reyn.tools.invoke_skill import (
    _INVOKE_SKILL_DESCRIPTION,
    _INVOKE_SKILL_PARAMETERS,
    INVOKE_SKILL,
)

# ── 1. Canonical name (ADR-0026 Open Q #6) ───────────────────────────────────

def test_invoke_skill_canonical_name():
    """Tier 2: INVOKE_SKILL.name is 'invoke_skill' — the router-side name
    adopted as canonical per ADR-0026 Open Q #6. Phase-side 'run_skill' op kind
    continues as a backward-compat alias via OP_KIND_MODEL_MAP (deferred to M4)."""
    assert INVOKE_SKILL.name == "invoke_skill"


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_invoke_skill_gate_router_allow():
    """Tier 2: INVOKE_SKILL.gates.router == 'allow' — router LLM may call it."""
    assert INVOKE_SKILL.gates.router == "allow"


def test_invoke_skill_gate_phase_allow():
    """Tier 2: INVOKE_SKILL.gates.phase == 'allow' — phase control_ir may invoke it."""
    assert INVOKE_SKILL.gates.phase == "allow"


# ── 3. Description byte-identity ─────────────────────────────────────────────

def test_invoke_skill_description_byte_identical():
    """Tier 2: INVOKE_SKILL.description is byte-identical to the router_tools.py
    invoke_skill ToolSpec.description (= excluding dynamic enum content).
    Drift here would invalidate LLMReplay fixtures."""
    legacy_description = (
        "Run a skill from the registered list. "
        "The 'name' parameter MUST be one of the skills "
        "listed in the system prompt's \"Available skills\" "
        "section, used verbatim (no dots, no slashes, "
        "no namespace prefixes). "
        "Use list_skills' input_fields hint to construct "
        "the correct input, or call describe_skill for full "
        "schema details. Do not guess input field names."
    )
    assert INVOKE_SKILL.description == legacy_description


def test_invoke_skill_description_constant_matches_instance():
    """Tier 2: _INVOKE_SKILL_DESCRIPTION module constant matches
    INVOKE_SKILL.description. Ensures no accidental divergence between
    the constant and what the ToolDefinition instance holds."""
    assert INVOKE_SKILL.description == _INVOKE_SKILL_DESCRIPTION


# ── 4. Static base parameters shape ──────────────────────────────────────────

def test_invoke_skill_parameters_static_base_shape():
    """Tier 2: INVOKE_SKILL parameters static base shape has required 'name'
    and 'input' fields, with 'name' typed as string (no enum — enum is a
    per-call router-side enrichment not handled by render_for_router())."""
    params = dict(INVOKE_SKILL.parameters)
    assert params["type"] == "object"
    assert set(params["required"]) == {"name", "input"}

    props = params["properties"]
    # 'name' is "type": "string" without an enum in the static base shape.
    assert props["name"]["type"] == "string"
    # 'input' is "type": "object"
    assert props["input"]["type"] == "object"


def test_invoke_skill_parameters_no_static_enum_on_name():
    """Tier 2: INVOKE_SKILL static parameters do NOT include an 'enum' on
    the 'name' field. The per-call enum injection is a router-side concern
    (_invoke_skill_name_schema in router_tools.py) not handled by the registry
    render. This invariant documents expected absence, not an oversight."""
    props = dict(INVOKE_SKILL.parameters)["properties"]
    assert "enum" not in props["name"], (
        "Static base parameters must NOT contain a 'name' enum — "
        "per-call enum injection is a router-side concern (not registry render). "
        "See module docstring in invoke_skill.py for rationale."
    )


def test_invoke_skill_parameters_constant_matches_instance():
    """Tier 2: _INVOKE_SKILL_PARAMETERS module constant matches
    INVOKE_SKILL.parameters. Ensures no accidental divergence."""
    assert dict(INVOKE_SKILL.parameters) == _INVOKE_SKILL_PARAMETERS


# ── 5. Purity and category ────────────────────────────────────────────────────

def test_invoke_skill_purity_side_effect():
    """Tier 2: INVOKE_SKILL purity is 'side_effect' — invoking a skill has
    external / state-changing side effects (sub-skill runs, workspace writes)."""
    assert INVOKE_SKILL.purity == "side_effect"


def test_invoke_skill_category_invocation():
    """Tier 2: INVOKE_SKILL category is 'invocation'."""
    assert INVOKE_SKILL.category == "invocation"


# ── 6. render_for_router shape ───────────────────────────────────────────────

def test_invoke_skill_render_for_router_shape():
    """Tier 2: INVOKE_SKILL.render_for_router() produces the OpenAI tools[]
    entry shape with correct type, name, description, and static parameters."""
    rendered = INVOKE_SKILL.render_for_router()

    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "invoke_skill"
    assert fn["description"] == _INVOKE_SKILL_DESCRIPTION
    assert fn["parameters"]["type"] == "object"
    assert fn["parameters"]["required"] == ["name", "input"]


def test_invoke_skill_render_for_router_no_per_call_enum():
    """Tier 2: render_for_router() does NOT inject a per-call skill-name enum.
    The static base render is the base shape only; router_tools.py enrichment
    stays inline until M4 introduces a per-call render override mechanism."""
    rendered = INVOKE_SKILL.render_for_router()
    name_prop = rendered["function"]["parameters"]["properties"]["name"]
    assert "enum" not in name_prop


# ── 7. Registry lookup ────────────────────────────────────────────────────────

def test_default_registry_contains_invoke_skill():
    """Tier 2: get_default_registry() returns a registry that contains
    'invoke_skill'."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    assert "invoke_skill" in registry


def test_default_registry_lookup_returns_invoke_skill_instance():
    """Tier 2: registry.lookup('invoke_skill') returns the INVOKE_SKILL instance."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    found = registry.lookup("invoke_skill")
    assert found is INVOKE_SKILL


def test_default_registry_invoke_skill_in_for_router():
    """Tier 2: INVOKE_SKILL appears in registry.for_router() (gates.router=allow)."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    router_tools = registry.for_router()
    assert INVOKE_SKILL in router_tools


def test_default_registry_invoke_skill_in_for_phase():
    """Tier 2: INVOKE_SKILL appears in registry.for_phase() (gates.phase=allow)."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    phase_tools = registry.for_phase()
    assert INVOKE_SKILL in phase_tools
