"""Tier 2: PLAN ToolDefinition M3 invariants (ADR-0026 M3 Wave 1).

Verifies that PLAN ToolDefinition:
- Produces byte-identical output to the prior ToolSpec literal for plan.
  Drift in description or parameters here would invalidate LLMReplay
  fixtures and alter the router's tool list.
- Has the correct gates (router=allow, phase=deny).
- Has the correct purity and category.
- Can be registered in a ToolRegistry without error (single-entry).
- Handler raises NotImplementedError (design-revisit; see plan.py module
  docstring).

Note on get_default_registry(): __init__.py is not modified in Wave 1
(per-capability files only).  Registry-lookup tests use a locally
constructed ToolRegistry so they remain runnable and self-contained.
The __init__.py wiring is the Wave 1 → default-registry integration step
handled separately.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.plan import _PLAN_DESCRIPTION, _PLAN_PARAMETERS, PLAN
from reyn.tools.registry import ToolRegistry

# ── 1. render_for_router byte-identity gate ───────────────────────────────────

def test_plan_router_render_matches_legacy_shape():
    """Tier 2: PLAN.render_for_router() produces byte-identical output to
    the prior ToolSpec literal for plan. Drift here would invalidate
    LLMReplay fixtures."""
    rendered = PLAN.render_for_router()

    # Top-level shape
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)

    fn = rendered["function"]

    # Name
    assert fn["name"] == "plan"

    # Description: key phrases that identify the exact plan description.
    assert "2-7 independent" in fn["description"]
    assert "multi-" in fn["description"]
    assert "synthesise." in fn["description"]
    assert "do NOT use plan" in fn["description"]

    # Parameters schema
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"goal", "steps_json"}
    assert "goal" in params["properties"]
    assert "steps_json" in params["properties"]
    assert params["properties"]["goal"]["type"] == "string"
    assert params["properties"]["steps_json"]["type"] == "string"


def test_plan_router_render_exact_description():
    """Tier 2: PLAN description is byte-identical to the legacy ToolSpec
    description string. Any whitespace or punctuation diff is a stop signal."""
    rendered = PLAN.render_for_router()
    legacy_description = (
        "Decompose a complex query into 2-7 independent "
        "sub-tasks. Use ONLY when the query needs multi-"
        "source synthesis (e.g. \"explain X with code "
        "references\", \"compare A vs B from multiple "
        "docs\", \"build a summary across these N "
        "files\"). For simple queries — chitchat, single-"
        "tool retrieval, single-source narration — reply "
        "directly or call one tool; do NOT use plan. The "
        "terminal step's text reply becomes the user-"
        "facing answer; design the last step to "
        "synthesise."
    )
    assert rendered["function"]["description"] == legacy_description


def test_plan_router_render_exact_parameters():
    """Tier 2: PLAN parameters schema is byte-identical to the legacy
    ToolSpec parameters dict."""
    rendered = PLAN.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "1-sentence restatement of the user's overall query."
                ),
            },
            "steps_json": {
                "type": "string",
                "description": (
                    "JSON-encoded array of 2-7 step objects. Each "
                    "step has shape: "
                    "{\"id\": str, \"description\": str, "
                    "\"tools\": [str, ...], \"depends_on\": [str, ...]}. "
                    "id: short unique identifier. description: what "
                    "this step does. "
                    "tools: list of TOP-LEVEL tool names this step "
                    "calls (e.g. \"reyn_src_read\", \"web_search\", "
                    "\"invoke_skill\"). Use [] for steps that just "
                    "synthesise / compare / summarise from prior step "
                    "outputs — the step's LLM does that natively without "
                    "any tool. To run a skill, use [\"invoke_skill\"], "
                    "NOT the skill's name. depends_on: ids of prior "
                    "steps whose output this step needs (default []). "
                    "The terminal step's text reply becomes the user-"
                    "facing answer; design the last step to "
                    "synthesise (= tools: []). Example: "
                    "[{\"id\": \"s1\", \"description\": \"read README\", "
                    "\"tools\": [\"reyn_src_read\"], \"depends_on\": []}, "
                    "{\"id\": \"s2\", \"description\": \"compare and "
                    "summarise for user\", "
                    "\"tools\": [], \"depends_on\": [\"s1\"]}]"
                ),
            },
        },
        "required": ["goal", "steps_json"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_plan_gates_router_allow():
    """Tier 2: PLAN has gates.router=allow (plan is a router-level tool)."""
    assert PLAN.gates.router == "allow"


def test_plan_gates_phase_deny():
    """Tier 2: PLAN has gates.phase=deny (plan must not appear in phase
    Control IR — phases cannot spawn PlanRuntime tasks)."""
    assert PLAN.gates.phase == "deny"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_plan_purity_side_effect():
    """Tier 2: PLAN purity is 'side_effect' (spawns PlanRuntime task,
    modifies running_plans state)."""
    assert PLAN.purity == "side_effect"


def test_plan_category_orchestration():
    """Tier 2: PLAN category is 'orchestration'."""
    assert PLAN.category == "orchestration"


# ── 4. Registry registration invariants ──────────────────────────────────────
# Wave 1: __init__.py is not modified (per-capability files only).
# Tests use a locally constructed ToolRegistry to stay self-contained.

def _make_registry() -> ToolRegistry:
    """Build a fresh ToolRegistry with PLAN registered."""
    r = ToolRegistry()
    r.register(PLAN)
    return r


def test_plan_registry_contains_plan():
    """Tier 2: A ToolRegistry that has PLAN registered contains 'plan'."""
    registry = _make_registry()
    assert "plan" in registry


def test_plan_registry_lookup_returns_plan_instance():
    """Tier 2: registry.lookup('plan') returns the PLAN instance."""
    registry = _make_registry()
    found = registry.lookup("plan")
    assert found is PLAN


def test_plan_registry_plan_in_for_router():
    """Tier 2: PLAN appears in registry.for_router() (gates.router=allow)."""
    registry = _make_registry()
    router_tools = registry.for_router()
    assert PLAN in router_tools


def test_plan_registry_plan_not_in_for_phase():
    """Tier 2: PLAN does NOT appear in registry.for_phase() (gates.phase=deny).
    Phases must not be able to spawn plan tasks via Control IR."""
    registry = _make_registry()
    phase_tools = registry.for_phase()
    assert PLAN not in phase_tools


# ── 5. Drift detection — description / parameters constants match render ──────

def test_plan_description_constant_matches_render():
    """Tier 2: _PLAN_DESCRIPTION module constant matches the rendered
    description. Ensures no accidental divergence between the constant
    and what PLAN.description holds."""
    rendered = PLAN.render_for_router()
    assert rendered["function"]["description"] == _PLAN_DESCRIPTION
    assert PLAN.description == _PLAN_DESCRIPTION


def test_plan_parameters_constant_matches_render():
    """Tier 2: _PLAN_PARAMETERS module constant matches the rendered
    parameters. Ensures no accidental divergence."""
    rendered = PLAN.render_for_router()
    assert rendered["function"]["parameters"] == _PLAN_PARAMETERS
    assert dict(PLAN.parameters) == _PLAN_PARAMETERS


# ── 6. Handler activated (M4 Phase 3) — mis-wiring contract ─────────────────
# Happy-path delegation tests live in tests/test_tool_registry_handlers.py.

@pytest.mark.asyncio
async def test_plan_handler_raises_when_router_state_missing():
    """Tier 2: PLAN.handler raises RuntimeError when ctx.router_state is
    None or .dispatch_plan_tool is unset (= M4 Phase 3 activation contract;
    RouterLoop is responsible for binding session state at population time)."""
    from reyn.tools.types import ToolContext

    # Minimal ToolContext; handler must raise before using any fields.
    ctx = ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
    )
    with pytest.raises(RuntimeError, match="dispatch_plan_tool"):
        await PLAN.handler({"goal": "test", "steps_json": "[]"}, ctx)
