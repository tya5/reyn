"""Tier 2: DELEGATE_TO_AGENT ToolDefinition ADR-0026 M3 Wave 1 invariants.

Verifies that DELEGATE_TO_AGENT ToolDefinition:
- Produces byte-identical render_for_router() output to the legacy ToolSpec
  literal for delegate_to_agent (description + static parameters).
- Has gates.router=allow and gates.phase=deny (router-only capability).
- Has the correct purity and category.
- Is registerable in a ToolRegistry without error.
- Is returned by for_router() and excluded from for_phase().
- Exposes the async-dispatch semantics via the NotImplementedError contract
  (= handler raises if accidentally called as a standalone adapter).

Note on registry: DELEGATE_TO_AGENT is NOT in get_default_registry() because
__init__.py may not be modified in Wave 1. Tests that require a registry
construct a local ToolRegistry instance and register DELEGATE_TO_AGENT
directly — this is the correct approach per the DO NOT Touch constraint.

Note on dynamic enum: The legacy build_tools() ToolSpec injects a runtime
"enum" on the "to" property based on available_agents. The unified
ToolDefinition uses the static base type {"type": "string"} per ADR-0026
M3 Wave 1 scope. Wave 2 should document how per-call schema narrowing
interacts with the unified registry (ADR-0026 Open Question #6 candidate).

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.delegate_to_agent import (
    _DELEGATE_TO_AGENT_DESCRIPTION,
    _DELEGATE_TO_AGENT_PARAMETERS,
    DELEGATE_TO_AGENT,
)
from reyn.tools.registry import ToolRegistry

# ── 1. render_for_router byte-identity gate ───────────────────────────────────

def test_delegate_to_agent_router_render_shape():
    """Tier 2: DELEGATE_TO_AGENT.render_for_router() has the correct top-level
    shape expected by the LiteLLM tools[] argument."""
    rendered = DELEGATE_TO_AGENT.render_for_router()
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)
    fn = rendered["function"]
    assert fn["name"] == "delegate_to_agent"
    assert isinstance(fn["description"], str)
    assert isinstance(fn["parameters"], dict)


def test_delegate_to_agent_router_render_exact_description():
    """Tier 2: DELEGATE_TO_AGENT description is byte-identical to the legacy
    ToolSpec description string. Any character diff is a stop signal for
    LLMReplay fixture compatibility."""
    rendered = DELEGATE_TO_AGENT.render_for_router()
    assert rendered["function"]["description"] == "Forward the request to a peer agent."


def test_delegate_to_agent_router_render_exact_parameters():
    """Tier 2: DELEGATE_TO_AGENT parameters schema matches the legacy ToolSpec
    parameters (static form, without the per-call enum narrowing)."""
    rendered = DELEGATE_TO_AGENT.render_for_router()
    params = rendered["function"]["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["to", "request"]
    assert "to" in params["properties"]
    assert "request" in params["properties"]
    # "to" is the static base type — no enum at registry level
    assert params["properties"]["to"]["type"] == "string"
    assert params["properties"]["request"]["type"] == "string"


def test_delegate_to_agent_description_constant_matches_render():
    """Tier 2: _DELEGATE_TO_AGENT_DESCRIPTION module constant matches the
    rendered description. Guards against accidental divergence between the
    constant and what DELEGATE_TO_AGENT.description holds."""
    rendered = DELEGATE_TO_AGENT.render_for_router()
    assert rendered["function"]["description"] == _DELEGATE_TO_AGENT_DESCRIPTION
    assert DELEGATE_TO_AGENT.description == _DELEGATE_TO_AGENT_DESCRIPTION


def test_delegate_to_agent_parameters_constant_matches_render():
    """Tier 2: _DELEGATE_TO_AGENT_PARAMETERS module constant matches the
    rendered parameters. Guards against accidental divergence."""
    rendered = DELEGATE_TO_AGENT.render_for_router()
    assert rendered["function"]["parameters"] == _DELEGATE_TO_AGENT_PARAMETERS
    assert dict(DELEGATE_TO_AGENT.parameters) == _DELEGATE_TO_AGENT_PARAMETERS


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_delegate_to_agent_gates_router_allow():
    """Tier 2: DELEGATE_TO_AGENT has gates.router=allow (visible to router)."""
    assert DELEGATE_TO_AGENT.gates.router == "allow"


def test_delegate_to_agent_gates_phase_deny():
    """Tier 2: DELEGATE_TO_AGENT has gates.phase=deny (not visible to phase).
    Phase does not have a peer-agent-message concept; deny is correct."""
    assert DELEGATE_TO_AGENT.gates.phase == "deny"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_delegate_to_agent_purity_side_effect():
    """Tier 2: DELEGATE_TO_AGENT purity is 'side_effect' (dispatches to a
    peer agent — external observable effect)."""
    assert DELEGATE_TO_AGENT.purity == "side_effect"


def test_delegate_to_agent_category_delegation():
    """Tier 2: DELEGATE_TO_AGENT category is 'delegation'."""
    assert DELEGATE_TO_AGENT.category == "delegation"


# ── 4. Registry operations ────────────────────────────────────────────────────

def test_delegate_to_agent_registers_without_error():
    """Tier 2: DELEGATE_TO_AGENT can be registered in a ToolRegistry without
    raising. Guards against type-level contract violations."""
    registry = ToolRegistry()
    registry.register(DELEGATE_TO_AGENT)  # must not raise
    assert "delegate_to_agent" in registry


def test_delegate_to_agent_registry_lookup():
    """Tier 2: registry.lookup('delegate_to_agent') returns the
    DELEGATE_TO_AGENT instance after registration."""
    registry = ToolRegistry()
    registry.register(DELEGATE_TO_AGENT)
    found = registry.lookup("delegate_to_agent")
    assert found is DELEGATE_TO_AGENT


def test_delegate_to_agent_in_for_router_not_in_for_phase():
    """Tier 2: DELEGATE_TO_AGENT appears in registry.for_router() but NOT in
    registry.for_phase(). Confirms router=allow / phase=deny gating."""
    registry = ToolRegistry()
    registry.register(DELEGATE_TO_AGENT)
    assert DELEGATE_TO_AGENT in registry.for_router()
    assert DELEGATE_TO_AGENT not in registry.for_phase()


# ── 5. Handler activated (M4 Phase 3) — mis-wiring contract ──────────────────
# Happy-path delegation tests live in tests/test_tool_registry_handlers.py.

@pytest.mark.asyncio
async def test_delegate_to_agent_handler_raises_when_router_state_missing():
    """Tier 2: DELEGATE_TO_AGENT.handler raises RuntimeError when
    ctx.router_state is None or .send_to_agent is unset (= M4 Phase 3
    activation contract; RouterLoop binds chain_id at population time and
    the handler passes only per-call args)."""
    from reyn.tools.types import ToolContext

    # Minimal stub context — handler should raise before touching ctx.
    ctx = ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
    )
    with pytest.raises(RuntimeError, match="send_to_agent"):
        await DELEGATE_TO_AGENT.handler(
            args={"to": "some_agent", "request": "hello"},
            ctx=ctx,
        )
