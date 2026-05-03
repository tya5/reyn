"""Tests for explicit skill-level permissions declaration (案 2 migration).

Tier 2: OS invariant — pin the contract that:
  (a) A skill with an explicit `permissions:` block in skill.md frontmatter
      uses that declaration directly (NOT the phase union).
  (b) A skill without a `permissions:` block falls back to the phase union
      (existing behavior, backward-compatible with non-migrated skills).
  (c) `permissions: {}` (empty mapping) is treated as "no explicit decl"
      via `or {}` semantics (= falls back to phase union, same as absent).

No mocks, no private state.  Real IR + expander objects only.
"""
from __future__ import annotations

import pytest

from reyn.compiler.expander import expand_phase, expand_skill
from reyn.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.permissions.permissions import PermissionDecl


# ── Helpers ───────────────────────────────────────────────────────────────────


def _artifacts() -> dict[str, ArtifactDef]:
    return {
        "in_art": ArtifactDef(
            name="in_art",
            schema={"type": "object", "properties": {}},
            description="",
            wrapped=True,
        ),
        "out_art": ArtifactDef(
            name="out_art",
            schema={"type": "object", "properties": {}},
            description="",
            wrapped=True,
        ),
    }


def _phase(name: str, permissions: dict, *, can_finish: bool = False) -> PhaseDef:
    return PhaseDef(
        name=name,
        inputs=["in_art"],
        role=None,
        can_finish=can_finish,
        instructions="",
        permissions=permissions,
    )


def _build(
    phase_perms: dict[str, dict],
    *,
    skill_permissions: dict | None = None,
):
    """Build a Skill from per-phase permission dicts and optional skill-level perms.

    `skill_permissions=None` → SkillDef.permissions defaults to {} (no explicit decl).
    """
    arts = _artifacts()
    phase_defs: dict[str, PhaseDef] = {}
    phase_objects = {}
    for name, perms in phase_perms.items():
        pd = _phase(name, perms, can_finish=(name == list(phase_perms)[-1]))
        phase_defs[name] = pd
        phase_objects[name] = expand_phase(pd, [arts["in_art"]])

    names = list(phase_perms)
    edges = [(names[i], names[i + 1]) for i in range(len(names) - 1)]

    sd = SkillDef(
        name="test_skill",
        description="",
        doc="",
        entry=names[0],
        edges=edges,
        skill_nodes={},
        final_output="out_art",
        final_output_description="",
        finish_criteria=[],
        permissions=skill_permissions if skill_permissions is not None else {},
    )
    return expand_skill(sd, phase_defs, arts, phase_objects)


# ── (a) Explicit skill-level permissions win over phase union ─────────────────


def test_explicit_skill_permissions_override_phase_union() -> None:
    """Tier 2: Explicit skill.permissions wins — phase union is NOT used.

    Phase declares mcp=['fs']; skill declares mcp=['override'].
    Result must be ['override'], NOT ['fs'] or ['override','fs'].
    """
    skill = _build(
        {"a": {"mcp": ["fs"]}, "b": {}},
        skill_permissions={"mcp": ["override"]},
    )
    assert skill.permissions.mcp == ["override"]
    # phase-level decls remain intact
    assert skill.phases["a"].permissions.mcp == ["fs"]


def test_explicit_skill_permissions_shell_true() -> None:
    """Tier 2: Explicit skill.permissions.shell=True propagates even when no phase declares it."""
    skill = _build(
        {"a": {}, "b": {}},
        skill_permissions={"shell": True},
    )
    assert skill.permissions.shell is True


def test_explicit_skill_permissions_python_used_directly() -> None:
    """Tier 2: Explicit skill.permissions.python is used verbatim, not merged with phase union."""
    skill = _build(
        {
            "a": {
                "python": [
                    {"module": "m", "function": "f_phase", "mode": "pure", "timeout": 30}
                ]
            }
        },
        skill_permissions={
            "python": [
                {"module": "m", "function": "f_skill", "mode": "pure", "timeout": 10}
            ]
        },
    )
    fns = [p.function for p in skill.permissions.python]
    # Only the skill-level decl; the phase-only function is NOT included
    assert fns == ["f_skill"]


def test_explicit_skill_permissions_file_write() -> None:
    """Tier 2: Explicit file.write at skill level used directly."""
    skill = _build(
        {"a": {}},
        skill_permissions={
            "file.write": [{"path": "reyn/local", "scope": "recursive"}]
        },
    )
    assert len(skill.permissions.file_write) == 1
    assert skill.permissions.file_write[0]["path"] == "reyn/local"
    assert skill.permissions.file_write[0]["scope"] == "recursive"


# ── (b) No skill-level permissions → falls back to phase union ────────────────


def test_no_skill_permissions_falls_back_to_phase_union() -> None:
    """Tier 2: Absent skill.permissions → phase union (existing behavior preserved)."""
    skill = _build(
        {"a": {"mcp": ["fs", "web"]}, "b": {"mcp": ["web", "search"]}},
        skill_permissions=None,  # no explicit decl
    )
    # Phase union: de-dup, order-preserving
    assert skill.permissions.mcp == ["fs", "web", "search"]


# ── (c) Empty-dict permissions treated as absent → phase union ────────────────


def test_empty_dict_skill_permissions_treated_as_absent() -> None:
    """Tier 2: permissions={} (explicit empty mapping) is treated as no explicit decl.

    `fm.get('permissions') or {}` semantics: both missing key AND empty dict
    result in the fallback path (phase union), so {} does NOT produce an
    empty PermissionDecl that silently erases phase-level perms.
    """
    skill = _build(
        {"a": {"mcp": ["fs"]}},
        skill_permissions={},  # empty dict → treated as "no explicit decl"
    )
    # Falls back to phase union → mcp=['fs']
    assert skill.permissions.mcp == ["fs"]
