"""Tests for Skill.permissions auto-union from phase permissions.

Tier 2: OS invariant — pin the contract that Skill.permissions is the
union of all declared phase permissions, computed by the expander at
skill construction time. This is the foundation for skill-level
permission gating (postprocessor + future skill-wide hooks) without
requiring a hard migration of the phase-level frontmatter syntax.

These tests construct PhaseDef/SkillDef objects directly and run the
expander, observing the resulting Skill via its public attributes.
No mocks, no private state.
"""
from __future__ import annotations

import pytest

from reyn.compiler.expander import expand_phase, expand_skill
from reyn.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.permissions.permissions import PermissionDecl, PythonPermission
from reyn.schemas.models import Phase


# ── Helpers ───────────────────────────────────────────────────────────────────


def _empty_artifacts() -> dict[str, ArtifactDef]:
    return {
        "input_art": ArtifactDef(
            name="input_art",
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


def _phase_def(name: str, permissions: dict, *, can_finish: bool = False) -> PhaseDef:
    return PhaseDef(
        name=name,
        inputs=["input_art"],
        role=None,
        can_finish=can_finish,
        instructions="",
        permissions=permissions,
    )


def _skill_def(entry: str, edges: list[tuple[str, str]] = None) -> SkillDef:
    return SkillDef(
        name="test_skill",
        description="",
        doc="",
        entry=entry,
        edges=edges or [],
        skill_nodes={},
        final_output="out_art",
        final_output_description="",
        finish_criteria=[],
    )


def _build_skill(phase_perm_map: dict[str, dict]):
    """Construct a Skill with the given per-phase permission dicts.

    Returns the built Skill so tests can observe Skill.permissions directly.
    """
    artifacts = _empty_artifacts()
    phase_defs: dict[str, PhaseDef] = {}
    phase_objects: dict[str, Phase] = {}
    for name, perms in phase_perm_map.items():
        pd = _phase_def(name, perms, can_finish=(name == "finish"))
        phase_defs[name] = pd
        phase_objects[name] = expand_phase(pd, [artifacts["input_art"]])

    edges: list[tuple[str, str]] = []
    names = list(phase_perm_map.keys())
    if len(names) >= 2:
        edges = [(names[0], names[1])]
    sd = _skill_def(entry=names[0], edges=edges)
    return expand_skill(sd, phase_defs, artifacts, phase_objects)


# ── Tier 2: empty case ────────────────────────────────────────────────────────


def test_skill_permissions_empty_when_no_phase_declares() -> None:
    """Tier 2: Skill.permissions is empty PermissionDecl when no phase declares anything."""
    skill = _build_skill({"a": {}})
    perms = skill.permissions
    assert perms.shell is False
    assert perms.mcp == []
    assert perms.tool == []
    assert perms.file_read == []
    assert perms.file_write == []
    assert perms.python == []
    assert perms.allowed_mcp is None


# ── Tier 2: shell union ───────────────────────────────────────────────────────


def test_skill_permissions_shell_true_if_any_phase_declares() -> None:
    """Tier 2: skill.permissions.shell is True if any phase declares shell: true."""
    skill = _build_skill({
        "a": {},
        "b": {"shell": True},
    })
    assert skill.permissions.shell is True


def test_skill_permissions_shell_false_when_no_phase_declares() -> None:
    """Tier 2: skill.permissions.shell stays False when no phase declares it."""
    skill = _build_skill({
        "a": {"mcp": ["fs"]},
        "b": {},
    })
    assert skill.permissions.shell is False


# ── Tier 2: mcp / tool union (de-duplicated, order preserved) ─────────────────


def test_skill_permissions_mcp_union_dedup_preserves_order() -> None:
    """Tier 2: skill.permissions.mcp = de-duplicated union, first appearance wins order."""
    skill = _build_skill({
        "a": {"mcp": ["fs", "web"]},
        "b": {"mcp": ["web", "search"]},
    })
    assert skill.permissions.mcp == ["fs", "web", "search"]


def test_skill_permissions_tool_union_dedup_preserves_order() -> None:
    """Tier 2: skill.permissions.tool = de-duplicated union."""
    skill = _build_skill({
        "a": {"tool": ["greet"]},
        "b": {"tool": ["lookup", "greet"]},
    })
    assert skill.permissions.tool == ["greet", "lookup"]


# ── Tier 2: file_read / file_write union by (path, scope) ─────────────────────


def test_skill_permissions_file_read_union_by_path_scope() -> None:
    """Tier 2: file_read entries de-dup by (path, scope) tuple."""
    skill = _build_skill({
        "a": {"file.read": [{"path": "/etc/foo", "scope": "just_path"}]},
        "b": {"file.read": [
            {"path": "/etc/foo", "scope": "just_path"},   # dup
            {"path": "/etc/foo", "scope": "recursive"},   # different scope
            {"path": "/etc/bar", "scope": "just_path"},   # new path
        ]},
    })
    paths_scopes = [
        (e["path"], e["scope"]) for e in skill.permissions.file_read
    ]
    assert paths_scopes == [
        ("/etc/foo", "just_path"),
        ("/etc/foo", "recursive"),
        ("/etc/bar", "just_path"),
    ]


def test_skill_permissions_file_write_union_by_path_scope() -> None:
    """Tier 2: file_write entries de-dup by (path, scope) tuple."""
    skill = _build_skill({
        "a": {"file.write": [{"path": "/var/out", "scope": "recursive"}]},
        "b": {"file.write": [{"path": "/var/out", "scope": "recursive"}]},
    })
    assert len(skill.permissions.file_write) == 1
    assert skill.permissions.file_write[0]["path"] == "/var/out"
    assert skill.permissions.file_write[0]["scope"] == "recursive"


# ── Tier 2: python permissions union ─────────────────────────────────────────


def test_skill_permissions_python_union_by_module_function_mode() -> None:
    """Tier 2: python entries de-dup by (module, function, mode)."""
    skill = _build_skill({
        "a": {"python": [
            {"module": "m", "function": "f", "mode": "pure", "timeout": 10},
        ]},
        "b": {"python": [
            {"module": "m", "function": "f", "mode": "pure", "timeout": 99},   # dup
            {"module": "m", "function": "g", "mode": "pure", "timeout": 30},   # new function
        ]},
    })
    triples = [(p.module, p.function, p.mode) for p in skill.permissions.python]
    assert triples == [("m", "f", "pure"), ("m", "g", "pure")]
    # First-seen timeout wins (10, not 99)
    first = next(p for p in skill.permissions.python if p.function == "f")
    assert first.timeout == 10


def test_skill_permissions_python_distinguishes_modes() -> None:
    """Tier 2: python entries with same (module, function) but different mode are kept separate."""
    skill = _build_skill({
        "a": {"python": [
            {"module": "m", "function": "f", "mode": "pure", "timeout": 30},
            {"module": "m", "function": "f", "mode": "trusted", "timeout": 30},
        ]},
    })
    assert len(skill.permissions.python) == 2
    modes = sorted(p.mode for p in skill.permissions.python)
    assert modes == ["pure", "trusted"]


# ── Tier 2: phase-level permissions remain accessible ────────────────────────


def test_phase_permissions_unchanged_by_skill_union() -> None:
    """Tier 2: aggregating into Skill.permissions does not mutate Phase.permissions.

    Phase-level decls remain the per-phase upper bound used by existing
    callsites (require_*, preprocessor_executor); the skill-level decl is
    additive aggregation, not a replacement.
    """
    skill = _build_skill({
        "a": {"mcp": ["fs"]},
        "b": {"mcp": ["web"]},
    })
    # Per-phase decls preserved
    assert skill.phases["a"].permissions.mcp == ["fs"]
    assert skill.phases["b"].permissions.mcp == ["web"]
    # Skill-level union
    assert sorted(skill.permissions.mcp) == ["fs", "web"]
