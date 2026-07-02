"""Tier 2: #2415 gap — a declared ``scope: recursive`` write grant is honored on approval so SUBPATH
writes are covered, on the SAME running resolver, even when cwd != project_root.

The #2415 grant made startup_guard PROMPT for skill_builder's ``reyn/local`` write, but the write
still hit ``permission_denied: declared but not granted``. tui's live evidence isolated TWO roots:

  #1 (choice): an affirmative [y]/[j] persisted the EXACT declared path ``reyn/local``, which does
     not cover the real write target ``reyn/local/{name}/skill.md`` (a SUBPATH). Only [r] worked.
  #2 (path-resolution): even the persisted recursive grant (``reyn/local/``) was denied on the SAME
     run because ``_is_path_approved_for`` resolved the approved KEY relative to CWD (``_expand``)
     while resolving the check target relative to ``project_root`` — a run launched from a
     subdirectory (cwd != project_root) failed the ``relative_to`` match even with the grant in
     ``_saved``.

Fixes: (#1) honor the declared recursive scope on any affirmative approval; (#2) resolve the approved
key against ``project_root`` (approvals.yaml is project-scoped), matching the check target's base.

The original #2415 test used an approving bus on ONE resolver with cwd==project_root and an exact-path
expectation — it masked BOTH roots (fake-backend-misses-integration). This test drives the REAL flow:
real startup_guard → real persist/session → require_file_write of a subpath, on the SAME resolver,
with **cwd != project_root** (mirroring the live Direct-CLI run).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.compiler.parser import _split_frontmatter
from reyn.intervention_choices import JUST_PATH, NO, RECURSIVE, YES
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.skill.skill_paths import resolve_skill_path
from reyn.user_intervention import InterventionAnswer

_SUBPATH = "reyn/local/my_new_skill/skill.md"  # a SUBPATH of the declared reyn/local dir


class _Bus:
    def __init__(self, choice: str) -> None:
        self.choice = choice

    async def request(self, iv):  # noqa: ANN001 — RequestBus.request
        return InterventionAnswer(choice_id=self.choice)


def _recursive_skill() -> Skill:
    ph = Phase(name="build", instructions="x",
               input_schema={"type": "object", "properties": {}}, allowed_ops=["write_file"])
    return Skill(name="skill_builder", entry_phase="build", phases={"build": ph},
                 graph=SkillGraph(transitions={}, can_finish_phases=["build"]),
                 final_output_schema={"type": "object", "properties": {}}, final_output_name="r",
                 permissions=PermissionDecl.from_dict({"file.write": [{"path": "reyn/local", "scope": "recursive"}]}))


def _project_and_cwd(tmp_path: Path, monkeypatch) -> Path:
    """Set up cwd != project_root (the live Direct-CLI condition #2 needs). Returns project_root."""
    project_root = tmp_path / "project"
    other_cwd = tmp_path / "elsewhere"
    project_root.mkdir()
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)  # cwd != project_root
    return project_root


def _resolver(project_root: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=project_root, interactive=True)


@pytest.mark.parametrize("choice", [YES, JUST_PATH, RECURSIVE])
@pytest.mark.asyncio
async def test_affirmative_approval_covers_subpath_same_resolver_cwd_differs(choice, tmp_path, monkeypatch):
    """Tier 2: CORE — ANY affirmative approval (y/j/r) of a recursive-declared grant lets the skill
    write a SUBPATH, on the SAME running resolver, with cwd != project_root (the live condition).
    RED before the fixes: [y]/[j] grant the exact path (#1); and even [r] is denied because the
    approved key is resolved CWD-relative while the target is project_root-relative (#2)."""
    project_root = _project_and_cwd(tmp_path, monkeypatch)
    r = _resolver(project_root)  # ONE resolver, as run_orchestrator threads self._perm
    await r.startup_guard(_recursive_skill(), "skill_builder", _Bus(choice))
    # subpath write, non-interactive gate on the SAME resolver — must not raise
    await r.require_file_write(_recursive_skill().permissions, _SUBPATH, "skill_builder", bus=None)


@pytest.mark.asyncio
async def test_deny_still_denies_the_subpath(tmp_path, monkeypatch):
    """Tier 2: the fixes do not over-grant — a [N]o answer still denies the subpath write."""
    project_root = _project_and_cwd(tmp_path, monkeypatch)
    r = _resolver(project_root)
    await r.startup_guard(_recursive_skill(), "skill_builder", _Bus(NO))
    with pytest.raises(PermissionError):
        await r.require_file_write(_recursive_skill().permissions, _SUBPATH, "skill_builder", bus=None)


@pytest.mark.asyncio
async def test_recursive_grant_does_not_widen_to_siblings_or_parent(tmp_path, monkeypatch):
    """Tier 2: no access-widening — approving reyn/local recursive covers ONLY paths under it. A
    sibling (reyn/other/…) and the project root itself stay denied. Guards the #2 anchor change from
    accidentally matching a broader path."""
    project_root = _project_and_cwd(tmp_path, monkeypatch)
    r = _resolver(project_root)
    await r.startup_guard(_recursive_skill(), "skill_builder", _Bus(RECURSIVE))
    for outside in ("reyn/other/x.md", "reyn/localX/x.md", "top.md"):
        with pytest.raises(PermissionError):
            await r.require_file_write(_recursive_skill().permissions, outside, "skill_builder", bus=None)


@pytest.mark.asyncio
async def test_session_approve_path_relative_honored_when_cwd_differs(tmp_path, monkeypatch):
    """Tier 2: sibling fix (fold-in) — ``session_approve_path`` (the up-front prompt-suppress writer)
    resolves a RELATIVE project-scoped grant against project_root too, so it is honored when
    cwd != project_root. RED before: it stored a CWD-resolved key that never matched the
    project_root-anchored check target (the same cwd-anchor class as _is_path_approved_for)."""
    project_root = _project_and_cwd(tmp_path, monkeypatch)
    r = _resolver(project_root)
    r.session_approve_path("reyn/local", "skill_builder", "file.write", recursive=True)
    await r.require_file_write(_recursive_skill().permissions, _SUBPATH, "skill_builder", bus=None)


def test_skill_builder_really_declares_reyn_local_recursive():
    """Tier 2: anchor — the real skill_builder skill.md declares reyn/local as a RECURSIVE write, so
    honor-declared-recursive is the mechanism that unblocks its subpath DSL writes."""
    skill_dir, _ = resolve_skill_path("skill_builder")
    fm, _ = _split_frontmatter((Path(skill_dir) / "skill.md").read_text(encoding="utf-8"))
    decl = PermissionDecl.from_dict(fm.get("permissions") or {})
    assert any(e.get("path") == "reyn/local" and e.get("scope") == "recursive" for e in decl.file_write), \
        "skill_builder declares reyn/local as a recursive write grant"
