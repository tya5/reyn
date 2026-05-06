"""Tier 2: startup_guard auto-approves declared file.read paths in non-interactive mode (G15).

Guards the G15 fix: in non-interactive mode (piped stdin / sub-skill execution),
startup_guard must session-approve every declared file.read path so that
workspace._resolve_read succeeds for paths outside CWD.

Without the fix, startup_guard silently skips the prompt (_prompt_file_access
returns False when not interactive) and records nothing — so the runtime denies
every read outside CWD, even for paths the skill author explicitly declared.

These are Tier 2 OS-invariant tests.  No mocks; real PermissionResolver and
PermissionDecl instances.  A minimal Skill stand-in carries the declarations.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.skill.skill_paths import stdlib_root
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention


# ── Helpers ───────────────────────────────────────────────────────────────────


def _resolver(project_root: Path, *, interactive: bool) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=project_root,
        interactive=interactive,
    )


class _RecordingBus:
    """Real InterventionBus that records requests and auto-denies (safety net)."""

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id="no")


@dataclass
class _FakeSkill:
    """Minimal Skill stand-in carrying a PermissionDecl.  No LLM wiring needed."""

    permissions: PermissionDecl
    # Unused by startup_guard but required for typing.
    name: str = "test_skill"


def _skill_with_read(paths: list[dict]) -> _FakeSkill:
    """Build a minimal fake Skill with the given file.read declarations."""
    decl = PermissionDecl(file_read=paths)
    return _FakeSkill(permissions=decl)


# ── Tier 2 tests ─────────────────────────────────────────────────────────────


def test_startup_guard_noninteractive_approves_declared_read_path(tmp_path, tmp_path_factory, monkeypatch):
    """Tier 2: startup_guard auto-approves declared file.read in non-interactive mode.

    Guards G15 fix part 1: in non-interactive mode, startup_guard must call
    session_approve_path for every declared file.read path outside the default
    read zone.  After startup_guard, is_read_allowed must return True.

    Uses a sibling tmp directory that is genuinely outside CWD (tmp_path_factory
    creates a separate tmp directory, not under tmp_path).  The stdlib path is
    now always in the default read zone (B11-NEW-1 fix), so it cannot be used
    as an example of an out-of-zone path.
    """
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    external = tmp_path_factory.mktemp("external")
    monkeypatch.chdir(cwd_dir)
    skill = _skill_with_read([
        {"path": str(external), "scope": "recursive"},
    ])
    perm = _resolver(cwd_dir, interactive=False)
    bus = _RecordingBus()

    # Before startup_guard: external path is outside CWD → denied
    target = str(external / "somefile.txt")
    assert not perm.is_read_allowed(target, skill_name="test_skill"), (
        "Pre-condition: external path must be denied before startup_guard"
    )

    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    # After startup_guard in non-interactive mode: path is auto-approved
    assert perm.is_read_allowed(target, skill_name="test_skill"), (
        "startup_guard must session-approve declared read paths in non-interactive mode"
    )
    # Bus was never prompted (non-interactive path silently auto-approves)
    assert bus.requests == [], (
        "startup_guard must NOT issue interactive prompts in non-interactive mode"
    )


def test_startup_guard_noninteractive_recursive_scope_covers_subtree(tmp_path, monkeypatch):
    """Tier 2: non-interactive startup_guard recursive approval covers all subtree paths.

    scope='recursive' must approve the directory AND every path beneath it —
    not just the declared path itself.

    Uses monkeypatch.chdir(tmp_path) so the stdlib path is outside CWD.
    """
    monkeypatch.chdir(tmp_path)
    stdlib_skills = stdlib_root() / "skills"
    skill = _skill_with_read([
        {"path": str(stdlib_skills), "scope": "recursive"},
    ])
    perm = _resolver(tmp_path, interactive=False)
    bus = _RecordingBus()
    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    # Direct child file
    assert perm.is_read_allowed(str(stdlib_skills / "direct_llm" / "skill.md"), skill_name="test_skill"), (
        "Recursive approval must cover direct child files"
    )
    # Deep subtree file
    assert perm.is_read_allowed(str(stdlib_skills / "eval_builder" / "phases" / "analyze_skill.md"), skill_name="test_skill"), (
        "Recursive approval must cover deep subtree files"
    )


def test_startup_guard_noninteractive_approval_is_skill_scoped(tmp_path, tmp_path_factory, monkeypatch):
    """Tier 2: non-interactive startup_guard auto-approval is skill-scoped.

    Approval for 'test_skill' must NOT extend to a different skill_name.
    This pins the isolation invariant of the permission system.

    Uses a sibling external directory (NOT stdlib, NOT under CWD) so the path
    is genuinely out-of-zone and approval is required.  The stdlib path is now
    always in the default read zone (B11-NEW-1 fix) and therefore readable by
    all skills without a skill-scoped approval.
    """
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    external = tmp_path_factory.mktemp("external_scoped")
    monkeypatch.chdir(cwd_dir)
    skill = _skill_with_read([
        {"path": str(external), "scope": "recursive"},
    ])
    perm = _resolver(cwd_dir, interactive=False)
    bus = _RecordingBus()
    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    target = str(external / "data.txt")
    assert perm.is_read_allowed(target, skill_name="test_skill")
    assert not perm.is_read_allowed(target, skill_name="other_skill"), (
        "Approval for test_skill must not grant access under other_skill"
    )


def test_startup_guard_noninteractive_does_not_approve_write_paths(tmp_path):
    """Tier 2: non-interactive startup_guard does NOT auto-approve declared write paths.

    Only file.read declarations are auto-approved in non-interactive mode.
    Write paths require explicit config approval or persisted consent.
    """
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    decl = PermissionDecl(
        file_read=[{"path": str(external_dir), "scope": "recursive"}],
        file_write=[{"path": str(external_dir), "scope": "recursive"}],
    )
    skill = _FakeSkill(permissions=decl)
    perm = _resolver(tmp_path, interactive=False)
    bus = _RecordingBus()
    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    # Read should be approved
    target_file = str(external_dir / "somefile.txt")
    assert perm.is_read_allowed(target_file, skill_name="test_skill"), (
        "file.read declared path must be auto-approved in non-interactive mode"
    )
    # Write must NOT be auto-approved
    assert not perm.is_write_allowed(target_file, skill_name="test_skill"), (
        "file.write declared path must NOT be auto-approved in non-interactive mode"
    )


def test_startup_guard_interactive_still_prompts(tmp_path, tmp_path_factory, monkeypatch):
    """Tier 2: startup_guard in interactive mode still issues a prompt (regression guard).

    The non-interactive auto-approve path must not suppress interactive prompts.
    In interactive mode, _prompt_file_access is called and returns True on YES.

    Uses a sibling external directory (NOT stdlib, NOT under CWD) so the path
    is genuinely out-of-zone and a prompt is expected.  The stdlib path is now
    always in the default read zone (B11-NEW-1 fix) and would not trigger a
    prompt even in interactive mode.
    """
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    external = tmp_path_factory.mktemp("external_interactive")
    monkeypatch.chdir(cwd_dir)
    skill = _skill_with_read([
        {"path": str(external), "scope": "recursive"},
    ])
    perm = _resolver(cwd_dir, interactive=True)

    class _AutoYesBus:
        def __init__(self):
            self.requests = []

        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            self.requests.append(iv)
            return InterventionAnswer(choice_id="yes")

    bus = _AutoYesBus()
    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    # The bus must have been called (interactive path prompts)
    assert len(bus.requests) >= 1, (
        "startup_guard in interactive mode must issue a prompt for out-of-zone paths"
    )


def test_startup_guard_noninteractive_skips_already_approved_paths(tmp_path, monkeypatch):
    """Tier 2: startup_guard skips paths that are already session-approved.

    A path that was already approved via session_approve_path must not be
    added again — idempotent guard (prevents duplicate session entries).

    Uses monkeypatch.chdir(tmp_path) so the stdlib path is outside CWD.
    """
    monkeypatch.chdir(tmp_path)
    stdlib_skills = stdlib_root() / "skills"
    skill = _skill_with_read([
        {"path": str(stdlib_skills), "scope": "recursive"},
    ])
    perm = _resolver(tmp_path, interactive=False)
    bus = _RecordingBus()

    # Pre-approve the path (simulates parent skill approval propagating)
    perm.session_approve_path(str(stdlib_skills), "test_skill", "file.read", recursive=True)

    # startup_guard must recognize the pre-approval and not re-add it
    asyncio.run(perm.startup_guard(skill, "test_skill", bus))

    target = str(stdlib_skills / "direct_llm" / "skill.md")
    assert perm.is_read_allowed(target, skill_name="test_skill"), (
        "Already-approved path must remain approved after startup_guard"
    )


def test_invoke_sub_skill_signature_accepts_permission_resolver():
    """Tier 2: invoke_sub_skill accepts a permission_resolver keyword argument.

    Guards G15 fix part 2: the run_skill handler must be able to pass
    ctx.permission_resolver to invoke_sub_skill.  If the parameter is absent,
    sub-skills run without a resolver and workspace._resolve_read denies all
    paths outside CWD — even those the parent approved.
    """
    import inspect
    from reyn.skill.sub_skill_runner import invoke_sub_skill

    sig = inspect.signature(invoke_sub_skill)
    assert "permission_resolver" in sig.parameters, (
        "invoke_sub_skill must accept a permission_resolver keyword argument (G15 fix)"
    )
    # Confirm it is keyword-only with a default of None
    param = sig.parameters["permission_resolver"]
    assert param.default is None, (
        "permission_resolver default must be None (backward-compatible)"
    )
