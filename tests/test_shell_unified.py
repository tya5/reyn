"""Tier 2: SHELL ToolDefinition M3 Wave 1 invariants (ADR-0026 M3).

Verifies that SHELL ToolDefinition:
- Has router="deny" (security boundary — shell is never exposed to the router).
- Has phase="allow" (the phase-side shell op kind is the only consumer).
- Has purity="side_effect" and category="execution".
- render_for_phase() produces the expected Control IR shape.
- render_for_router() is structurally valid even though the tool is gated deny.
- registry.for_router() EXCLUDES SHELL (gate enforcement).
- registry.for_phase() INCLUDES SHELL.
- Role-separation contract: router is a public surface; shell MUST NOT appear there.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.shell import SHELL, _SHELL_DESCRIPTION, _SHELL_PARAMETERS
from reyn.tools.registry import ToolRegistry


# ── 1. Gate invariants — the security boundary ───────────────────────────────

def test_shell_gate_router_deny():
    """Tier 2: SHELL.gates.router == "deny" — shell is a security boundary.
    The router is a public surface; shell MUST NOT be exposed there."""
    assert SHELL.gates.router == "deny"


def test_shell_gate_phase_allow():
    """Tier 2: SHELL.gates.phase == "allow" — shell is available to phases."""
    assert SHELL.gates.phase == "allow"


# ── 2. Purity and category ────────────────────────────────────────────────────

def test_shell_purity_side_effect():
    """Tier 2: SHELL purity is 'side_effect' — shell commands mutate external state."""
    assert SHELL.purity == "side_effect"


def test_shell_category_execution():
    """Tier 2: SHELL category is 'execution'."""
    assert SHELL.category == "execution"


# ── 3. render_for_phase — Control IR shape ────────────────────────────────────

def test_shell_render_for_phase_kind():
    """Tier 2: render_for_phase() produces kind="shell"."""
    rendered = SHELL.render_for_phase()
    assert rendered["kind"] == "shell"


def test_shell_render_for_phase_args_schema_cmd():
    """Tier 2: render_for_phase() args_schema contains required "cmd" field."""
    rendered = SHELL.render_for_phase()
    schema = rendered["args_schema"]
    assert schema["type"] == "object"
    assert "cmd" in schema["properties"]
    assert schema["properties"]["cmd"] == {"type": "string"}
    assert "cmd" in schema["required"]


def test_shell_render_for_phase_args_schema_timeout():
    """Tier 2: render_for_phase() args_schema contains optional "timeout" field."""
    rendered = SHELL.render_for_phase()
    schema = rendered["args_schema"]
    assert "timeout" in schema["properties"]
    assert schema["properties"]["timeout"] == {"type": "integer"}
    # timeout is not required (has a default of 120 in ShellIROp)
    assert "timeout" not in schema.get("required", [])


def test_shell_render_for_phase_purity():
    """Tier 2: render_for_phase() purity field matches SHELL.purity."""
    rendered = SHELL.render_for_phase()
    assert rendered["purity"] == "side_effect"


# ── 4. render_for_router — callable but structurally gated ───────────────────

def test_shell_render_for_router_is_callable():
    """Tier 2: render_for_router() is callable without error even though the
    gate is "deny". The gate refusal is enforced by registry.for_router(),
    not by the render method itself."""
    rendered = SHELL.render_for_router()
    assert rendered["type"] == "function"
    assert rendered["function"]["name"] == "shell"


def test_shell_excluded_from_registry_for_router():
    """Tier 2: registry.for_router() excludes SHELL (gates.router="deny").
    This is the role-separation contract: the router is a public surface
    and shell must never appear there."""
    registry = ToolRegistry()
    registry.register(SHELL)
    router_tools = registry.for_router()
    assert SHELL not in router_tools


# ── 5. registry.for_phase — SHELL is included ────────────────────────────────

def test_shell_included_in_registry_for_phase():
    """Tier 2: registry.for_phase() includes SHELL (gates.phase="allow")."""
    registry = ToolRegistry()
    registry.register(SHELL)
    phase_tools = registry.for_phase()
    assert SHELL in phase_tools


# ── 6. Role-separation contract — router vs phase gate asymmetry ──────────────

def test_shell_router_phase_gate_asymmetry():
    """Tier 2: SHELL has asymmetric gates (router=deny, phase=allow).
    This asymmetry is the intended security design: shell capability is
    never promoted to the router's public-facing tool surface."""
    assert SHELL.gates.router == "deny"
    assert SHELL.gates.phase == "allow"
    # Verify this is not a symmetric deny (= tool is accessible in at least one role)
    assert SHELL.gates.phase != "deny"


# ── 7. Description and parameters constants ───────────────────────────────────

def test_shell_description_constant_matches_definition():
    """Tier 2: _SHELL_DESCRIPTION module constant matches SHELL.description."""
    assert SHELL.description == _SHELL_DESCRIPTION


def test_shell_parameters_constant_matches_definition():
    """Tier 2: _SHELL_PARAMETERS module constant matches SHELL.parameters."""
    assert dict(SHELL.parameters) == _SHELL_PARAMETERS
