"""Tests for explicit skill-level permissions declaration (ADR-0020).

Tier 2: OS invariant — pin the contract that a skill with an explicit
`permissions:` block in skill.md frontmatter uses that declaration
directly as Skill.permissions (the single source of truth).

Phase-level `permissions:` is hard-rejected (see test_phase_permissions_rejected.py).
No mocks, no private state.  Real IR + expander objects only.
"""
from __future__ import annotations

from reyn.compiler.expander import expand_phase, expand_skill
from reyn.compiler.ir import ArtifactDef, PhaseDef, SkillDef

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


def _phase(name: str, *, can_finish: bool = False) -> PhaseDef:
    return PhaseDef(
        name=name,
        inputs=["in_art"],
        role=None,
        can_finish=can_finish,
        instructions="",
    )


def _build(
    phase_names: list[str],
    *,
    skill_permissions: dict | None = None,
):
    """Build a Skill from phase names and optional skill-level perms.

    `skill_permissions=None` → SkillDef.permissions defaults to {} (empty).
    """
    arts = _artifacts()
    phase_defs: dict[str, PhaseDef] = {}
    phase_objects = {}
    for i, name in enumerate(phase_names):
        pd = _phase(name, can_finish=(i == len(phase_names) - 1))
        phase_defs[name] = pd
        phase_objects[name] = expand_phase(pd, [arts["in_art"]])

    edges = [(phase_names[i], phase_names[i + 1]) for i in range(len(phase_names) - 1)]

    sd = SkillDef(
        name="test_skill",
        description="",
        doc="",
        entry=phase_names[0],
        edges=edges,
        skill_nodes={},
        final_output="out_art",
        final_output_description="",
        finish_criteria=[],
        permissions=skill_permissions if skill_permissions is not None else {},
    )
    return expand_skill(sd, phase_defs, arts, phase_objects)


# ── Explicit skill-level permissions are used directly ────────────────────────


def test_explicit_skill_permissions_mcp() -> None:
    """Tier 2: Explicit skill.permissions.mcp propagates to Skill.permissions."""
    skill = _build(["a", "b"], skill_permissions={"mcp": ["override"]})
    assert skill.permissions.mcp == ["override"]


def test_explicit_skill_permissions_shell_true() -> None:
    """Tier 2: Explicit skill.permissions.shell=True propagates."""
    skill = _build(["a", "b"], skill_permissions={"shell": True})
    assert skill.permissions.shell is True


def test_explicit_skill_permissions_python_used_directly() -> None:
    """Tier 2: Explicit skill.permissions.python is used verbatim."""
    skill = _build(
        ["a"],
        skill_permissions={
            "python": [
                {"module": "m", "function": "f_skill", "mode": "safe", "timeout": 10}
            ]
        },
    )
    fns = [p.function for p in skill.permissions.python]
    assert fns == ["f_skill"]


def test_explicit_skill_permissions_file_write() -> None:
    """Tier 2: Explicit file.write at skill level used directly."""
    skill = _build(
        ["a"],
        skill_permissions={
            "file.write": [{"path": "reyn/local", "scope": "recursive"}]
        },
    )
    assert len(skill.permissions.file_write) == 1
    assert skill.permissions.file_write[0]["path"] == "reyn/local"
    assert skill.permissions.file_write[0]["scope"] == "recursive"


def test_no_skill_permissions_yields_empty_decl() -> None:
    """Tier 2: Absent skill.permissions → empty PermissionDecl (no phase fallback)."""
    skill = _build(["a", "b"], skill_permissions=None)
    assert skill.permissions.shell is False
    assert skill.permissions.mcp == []
    assert skill.permissions.tool == []
