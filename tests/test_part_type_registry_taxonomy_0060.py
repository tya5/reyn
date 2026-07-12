"""Tier 2: proposal 0060 §3 F1 — part-type meta-registry taxonomy completeness
gate, mirroring the ``OP_KIND_MODEL_MAP`` <-> ``control-ir.md`` sync discipline
(CLAUDE.md hard rule) and ``BUILTIN_HOOK_SCHEMAS``'s registry-walk gate
(``tests/test_hook_event_schema_registry_sync_0059.py``).

``reyn.core.part_type_registry.PART_TYPE_REGISTRY`` is the single source of
truth for "every kind of part reyn has". This test WALKS that registry (never
a curated allowlist) and fails if any entry lacks a non-empty category, an
empty/invalid role set, or a malformed ``registry_ref`` shape — the
completeness discipline proposal 0060 §3 F1 calls for ("no curated subset").

The falsify test proves the gate is registry-DERIVED, not merely a fixed
assertion list: injecting a fake part-type with no category into a COPY of
the registry makes the completeness check fail; the real, unmodified registry
must still pass. This is the "add a part-type with no category -> gate RED"
proof the task calls for, done without mutating the module-level singleton
(so it can't leak into other tests importing the same module).
"""
from __future__ import annotations

import pytest

from reyn.core.part_type_registry import (
    PART_ROLES,
    PART_TYPE_REGISTRY,
    PartTypeSpec,
    all_part_types,
    part_types_for_role,
)


def _find_taxonomy_violations(registry: dict) -> list[str]:
    """Every completeness violation found while walking ``registry`` — empty
    list means the registry is fully catalogued. Registry-derived: iterates
    whatever the dict contains, never a hand-listed subset of names."""
    violations: list[str] = []
    for name, spec in registry.items():
        if not isinstance(spec, PartTypeSpec):
            violations.append(f"{name!r}: entry is not a PartTypeSpec")
            continue
        if not spec.category or not spec.category.strip():
            violations.append(f"{name!r}: missing/empty category")
        if not spec.roles:
            violations.append(f"{name!r}: empty role set")
        if spec.roles - set(PART_ROLES):
            violations.append(f"{name!r}: role(s) outside {PART_ROLES}: {spec.roles}")
        if ":" not in spec.registry_ref:
            violations.append(f"{name!r}: registry_ref {spec.registry_ref!r} is not module:qualname")
    return violations


def test_every_registered_part_type_has_a_category():
    """Tier 2: the real PART_TYPE_REGISTRY is fully catalogued today — walking
    it (not a hand-listed subset of expected names) finds zero violations."""
    violations = _find_taxonomy_violations(PART_TYPE_REGISTRY)
    assert not violations, f"taxonomy gate violations: {violations}"


def test_expected_part_types_are_present():
    """Tier 2: the 5 part-types the grounded current-state inventory (proposal
    §1.2-1.3) names as self-authorable are all registered — skill, pipeline,
    mcp, hook, presentation. ``task`` is deliberately excluded (§2.1:
    "task is excluded -- deprecating")."""
    assert set(all_part_types()) == {"skill", "pipeline", "mcp", "hook", "presentation"}
    assert "task" not in all_part_types()


def test_part_types_for_role_is_registry_derived():
    """Tier 2: ``part_types_for_role`` reflects the registry's own role
    declarations, not a hard-coded per-role list — mcp plays all three roles
    (proposal §2.1's matrix), hook is input-only, presentation is output-only."""
    assert "mcp" in part_types_for_role("input")
    assert "mcp" in part_types_for_role("workflow")
    assert "mcp" in part_types_for_role("output")
    assert part_types_for_role("input") == ("mcp", "hook")
    assert part_types_for_role("output") == ("mcp", "presentation")
    with pytest.raises(ValueError):
        part_types_for_role("not-a-role")


def test_taxonomy_gate_falsifies_on_uncatalogued_part_type():
    """Tier 2: falsify — registering a fake part-type with NO category (a copy
    of the registry, not the module singleton) turns the completeness check
    RED — proving the gate actually catches an uncatalogued part-type rather
    than only ever passing by construction. Restored immediately (a plain
    local dict copy; nothing module-level is mutated)."""
    assert _find_taxonomy_violations(PART_TYPE_REGISTRY) == []

    forged = dict(PART_TYPE_REGISTRY)
    forged["rogue_part"] = PartTypeSpec(
        name="rogue_part",
        roles=frozenset({"workflow"}),
        category="",  # <-- the defect: no category assigned
        registry_ref="some.module:thing",
        description="a part-type someone forgot to catalogue",
    )
    violations = _find_taxonomy_violations(forged)
    assert violations, "expected the forged uncatalogued part-type to fail the gate"
    assert any("rogue_part" in v and "category" in v for v in violations)

    # The real registry is untouched by the forgery above.
    assert _find_taxonomy_violations(PART_TYPE_REGISTRY) == []
