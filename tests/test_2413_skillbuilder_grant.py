"""Tier 2: #2413 — skill_builder declares its reyn/local write grant; stale zone text fixed.

skill_builder's ``build_skill`` (dsl_writer) writes the generated skill DSL under
``reyn/local/{name}/`` — OUTSIDE the ``.reyn/`` default write zone (#1505). It had NO permissions
block (sibling-sweep miss: #1519 added declared *read* grants to eval_builder but skill_builder,
which *writes*, was never given a write declaration) → the write was never surfaced at startup and
hit ``permission_denied`` mid-run.

File writes are deliberately decl-less at the gate (``#1199 S3.1c-1`` — ``require_file_write`` is
zone-OR-approved and never auto-grants from the decl). The declaration's real consumer is
``startup_guard`` (run_orchestrator wires it pre-flight): it scans ``skill.permissions.file_write``,
surfaces each out-of-zone path for approval upfront, and — once approved recursively — persists the
grant so the later build write passes. This pins that skill_builder's declared write grant makes the
reyn/local write *grantable* (surfaced + approvable), and that the stale default-zone message /
docstring (which still listed ``reyn/``) match the code (``.reyn/`` only). No mocks: real skill.md
frontmatter + real PermissionResolver + a real approving bus (not MagicMock).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.compiler.parser import _split_frontmatter
from reyn.intervention_choices import RECURSIVE
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.skill.skill_paths import resolve_skill_path
from reyn.user_intervention import InterventionAnswer


def _skill_builder_decl() -> PermissionDecl:
    skill_dir, _ = resolve_skill_path("skill_builder")
    fm, _ = _split_frontmatter((Path(skill_dir) / "skill.md").read_text(encoding="utf-8"))
    return PermissionDecl.from_dict(fm.get("permissions") or {})


def _skill(decl: PermissionDecl) -> Skill:
    ph = Phase(name="build_skill", instructions="x",
               input_schema={"type": "object", "properties": {}}, allowed_ops=["write_file"])
    return Skill(name="skill_builder", entry_phase="build_skill", phases={"build_skill": ph},
                 graph=SkillGraph(transitions={}, can_finish_phases=["build_skill"]),
                 final_output_schema={"type": "object", "properties": {}}, final_output_name="r",
                 permissions=decl)


class _ApprovingBus:
    """Real RequestBus stand-in — records prompts and approves each recursively.

    A concrete object, not a MagicMock: it satisfies the ``request`` contract with a real
    ``InterventionAnswer`` so the resolver's own approval/persist path runs unchanged.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def request(self, iv):  # noqa: ANN001 — matches RequestBus.request
        self.prompts.append(iv.prompt)
        return InterventionAnswer(choice_id=RECURSIVE)


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)


@pytest.mark.asyncio
async def test_skill_builder_declaration_makes_reyn_local_write_grantable(tmp_path, monkeypatch):
    """Tier 2: CORE — skill_builder's declared write grant makes the reyn/local DSL write grantable.

    startup_guard surfaces the declared out-of-zone reyn/local write for approval; approving it
    recursively persists the grant so the subsequent build write (bus=None) passes. RED without the
    grant: the write is never surfaced at startup and the later gate call denies it (see the sibling
    assertion below). Production has cwd == project_root, so chdir tmp_path here."""
    monkeypatch.chdir(tmp_path)
    decl = _skill_builder_decl()
    # The real skill.md must declare the reyn/local write (RED-when-stripped anchor).
    assert any(e.get("path") == "reyn/local" for e in decl.file_write), \
        "skill_builder must declare its reyn/local write grant"

    r = _resolver(tmp_path)
    bus = _ApprovingBus()
    await r.startup_guard(_skill(decl), "skill_builder", bus)
    # The declaration's effect: startup_guard surfaces the out-of-zone reyn/local write upfront.
    assert any("reyn/local" in p for p in bus.prompts), \
        "startup_guard surfaces the declared reyn/local write for approval"
    # Having approved it recursively, the actual build write now passes — even non-interactively.
    await r.require_file_write(decl, "reyn/local/my_new_skill/skill.md", "skill_builder", bus=None)


@pytest.mark.asyncio
async def test_without_declaration_reyn_local_write_is_not_surfaced_and_denied(tmp_path, monkeypatch):
    """Tier 2: the RED baseline — with NO write declaration, startup_guard never surfaces the
    reyn/local write and the mid-run gate denies it (the bug #2413 fixes). Isolates the declaration
    as the load-bearing difference vs the test above."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    bus = _ApprovingBus()
    await r.startup_guard(_skill(PermissionDecl()), "skill_builder", bus)
    assert not any("reyn/local" in p for p in bus.prompts), "no declaration → reyn/local not surfaced"
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), "reyn/local/my_new_skill/skill.md",
                                   "skill_builder", bus=None)


@pytest.mark.asyncio
async def test_default_write_zone_denial_message_says_only_dot_reyn(tmp_path, monkeypatch):
    """Tier 2: the outside-zone denial message names ``.reyn/`` ONLY — not the stale ``.reyn/, reyn/``
    (the code's default zone is `(".reyn",)`; #1505 dropped `reyn/`). Behavioral: the message a user
    sees on a genuinely-denied write."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError) as ei:
        await r.require_file_write(PermissionDecl(), "outside_zone/foo.txt", "x", bus=None)
    msg = str(ei.value)
    assert ".reyn/, reyn/" not in msg, "stale two-zone text gone"
    assert "reyn/)" not in msg or "(.reyn/)" in msg, "the zone shown is .reyn/ only"


@pytest.mark.asyncio
async def test_default_write_zone_is_dot_reyn_only_not_bare_reyn(tmp_path, monkeypatch):
    """Tier 2: behaviorally, the default write zone is ``.reyn/`` ONLY — a write under ``.reyn/`` is
    allowed with no declaration/approval, while a write under bare ``reyn/`` is denied (#1505 dropped
    ``reyn/`` from the zone; not reverted). Proves the code matches the corrected docstring/message via
    the public gate, not a private-constant read."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    # in-zone: .reyn/ write needs no declaration or approval
    await r.require_file_write(PermissionDecl(), ".reyn/scratch.txt", "x", bus=None)
    # out-of-zone: bare reyn/ (the dropped zone) is denied
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), "reyn/x.yaml", "x", bus=None)


def test_default_zone_docstring_not_stale():
    """Tier 2: the module docstring's default-write-zone line names .reyn/ only (not project/reyn/) —
    matches the code (drift-fix; #1505 tightening not reverted). Reads the public module ``__doc__``."""
    import reyn.security.permissions.permissions as perms

    assert (perms.__doc__ or "") and "project/reyn/ only" not in (perms.__doc__ or ""), \
        "stale 'project/reyn/' dropped from the default-zone docstring"
