"""Tier 2: #2415 root 3 — host workspace base_dir anchors on project_root, so the write TARGET and
the approval KEY resolve against the SAME base (full chain, cwd != project_root).

Roots 1/2 (#2420) fixed the approval-KEY base (project_root) + honored the declared recursive scope,
but the check-TARGET still resolved against ``workspace.base_dir`` which defaulted to CWD in host
mode — so a subdir invocation (cwd != project_root) split the two bases: ``file.py`` resolved the
write target under CWD while the approval resolved under project_root → the write was DENIED and,
worse, would have LANDED under CWD (wrong dir). Fix: ``build_environment_backend`` host mode returns
``ws_base_dir = project_root`` (== the permission zone base), aligning the write-location base with
the approval base.

These tests replicate the FULL chain (the prior root-1/2 tests used a relative target that resolved
via project_root and masked this): the funnel value → a real ``Workspace(base_dir=…)`` → the real
``file.py`` write op → the gate, with cwd != project_root.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from reyn.intervention_choices import RECURSIVE
from reyn.user_intervention import InterventionAnswer


def _project_and_cwd(tmp_path: Path, monkeypatch) -> Path:
    """A reyn project at tmp/project + cwd at a SUBDIR (tmp/project/docs) — the root-3 case: a
    subdir invocation where ``_find_project_root`` walks UP to the reyn.yaml ancestor, so cwd !=
    project_root but project_root IS discoverable."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("model: stub/model\n", encoding="utf-8")
    subdir = project_root / "docs"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    return project_root


def test_build_environment_backend_host_anchors_on_project_root(tmp_path, monkeypatch):
    """Tier 2: the funnel — host-mode ``build_environment_backend`` returns ws_base_dir == the project
    root (the reyn.yaml ancestor), NOT cwd. RED before the fix (returned None → Workspace default cwd)."""
    from reyn.interfaces.cli.env_backend import build_environment_backend

    project_root = _project_and_cwd(tmp_path, monkeypatch)
    _backend, ws_base_dir, _state, _cleanup = build_environment_backend(argparse.Namespace(env_backend="host"))
    assert ws_base_dir == project_root, "host workspace base_dir anchors on project_root, not cwd"


class _Bus:
    async def request(self, iv):  # noqa: ANN001
        return InterventionAnswer(choice_id=RECURSIVE)


def test_full_chain_write_lands_and_is_permitted_under_project_root(tmp_path, monkeypatch):
    """Tier 2: CORE full-chain — with the funnel's project_root base_dir, a real file.py write op to a
    reyn/local subpath (approved recursively) is PERMITTED and the file LANDS under project_root, with
    cwd != project_root. RED before the fix: base_dir=cwd → target resolves under cwd → not covered by
    the project_root approval (denied) and would land in the wrong dir."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.file import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.interfaces.cli.env_backend import build_environment_backend
    from reyn.schemas.models import FileIROp, Phase, Skill, SkillGraph
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

    project_root = _project_and_cwd(tmp_path, monkeypatch)
    _backend, ws_base_dir, _s, _c = build_environment_backend(argparse.Namespace(env_backend="host"))

    resolver = PermissionResolver(config_permissions={}, project_root=project_root,
                                  file_zone_root=ws_base_dir, interactive=True)
    # approve skill_builder's declared reyn/local recursive write (root-1/2 path)
    ph = Phase(name="b", instructions="x", input_schema={"type": "object", "properties": {}},
               allowed_ops=["write_file"])
    skill = Skill(name="skill_builder", entry_phase="b", phases={"b": ph},
                  graph=SkillGraph(transitions={}, can_finish_phases=["b"]),
                  final_output_schema={"type": "object", "properties": {}}, final_output_name="r",
                  permissions=PermissionDecl.from_dict({"file.write": [{"path": "reyn/local", "scope": "recursive"}]}))
    asyncio.run(resolver.startup_guard(skill, "skill_builder", _Bus()))

    events = EventLog()
    ws = Workspace(events, permission_resolver=resolver, skill_name="skill_builder", base_dir=ws_base_dir)
    ctx = OpContext(workspace=ws, events=events, permission_decl=skill.permissions,
                    permission_resolver=resolver, skill_name="skill_builder")
    op = FileIROp(kind="file", op="write", path="reyn/local/my_new_skill/skill.md", content="entry: x\n")
    result = asyncio.run(handle(op, ctx, "control_ir"))

    assert result.get("status") == "ok", "the write is PERMITTED (target base == approval base)"
    landed = project_root / "reyn" / "local" / "my_new_skill" / "skill.md"
    assert landed.exists(), "the file LANDS under project_root/reyn/local, not cwd"
    assert not (Path.cwd() / "reyn" / "local" / "my_new_skill" / "skill.md").exists(), \
        "the file did NOT land under cwd (the root-3 wrong-dir bug)"


def test_no_frontend_host_entry_hardcodes_workspace_base_dir_none():
    """Tier 2: completeness guard (#2415 root 3) — no frontend host-entry construction may hardcode
    ``workspace_base_dir=None``, which splits the write-target base (→ cwd) from the project_root-
    anchored permission zone. Host frontends must anchor base_dir on project_root (the
    build_environment_backend funnel value, or an explicit project_root as chainlit / mcp-serve do).
    This is the robust-by-construction regression guard (MCP-seam grep-gate pattern) — it caught the
    mcp-serve session factory the initial two-site sweep missed. RED if a new frontend reintroduces
    the split. (Signature defaults ``workspace_base_dir: "Path | None" = None`` are not matched — the
    call-site keyword form is contiguous ``workspace_base_dir=None``.)"""
    interfaces = Path(__file__).resolve().parents[1] / "src" / "reyn" / "interfaces"
    offenders = [
        f"{py.relative_to(interfaces)}:{i}"
        for py in interfaces.rglob("*.py")
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1)
        if "workspace_base_dir=None" in line
    ]
    assert not offenders, (
        "frontend host-entry hardcodes workspace_base_dir=None (splits base_dir from the "
        "project_root permission zone — anchor it on project_root): " + ", ".join(offenders)
    )


def test_no_frontend_host_entry_hardcodes_workspace_state_dir_none():
    """Tier 2: completeness guard (#2427) — no frontend host-entry construction may hardcode
    ``workspace_state_dir=None``, which causes events/WAL to resolve against cwd instead of
    project_root. Same base-split class as root 3 (workspace_base_dir), different param.
    Host frontends must use the build_environment_backend funnel value, or an explicit
    project_root/.reyn (as chainlit / mcp-serve do after #2427). RED if a new frontend
    reintroduces the split. (Signature defaults are not matched — only call-site keyword form.)"""
    interfaces = Path(__file__).resolve().parents[1] / "src" / "reyn" / "interfaces"
    offenders = [
        f"{py.relative_to(interfaces)}:{i}"
        for py in interfaces.rglob("*.py")
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1)
        if "workspace_state_dir=None" in line
    ]
    assert not offenders, (
        "frontend host-entry hardcodes workspace_state_dir=None (splits events/WAL from "
        "project_root — anchor it on project_root/.reyn): " + ", ".join(offenders)
    )


def test_build_environment_backend_host_state_dir_anchors_on_project_root(tmp_path, monkeypatch):
    """Tier 2: the funnel (#2427) — host-mode ``build_environment_backend`` returns
    ws_state_dir == project_root / '.reyn', NOT None. RED before the fix (returned None →
    SkillRuntime fell back to the relative string '.reyn' → events landed under cwd/.reyn
    instead of project_root/.reyn for subdir invocations)."""
    from reyn.interfaces.cli.env_backend import build_environment_backend

    project_root = _project_and_cwd(tmp_path, monkeypatch)
    _backend, _base, ws_state_dir, _cleanup = build_environment_backend(
        argparse.Namespace(env_backend="host")
    )
    assert ws_state_dir == project_root / ".reyn", (
        "host workspace_state_dir anchors on project_root/.reyn, not cwd"
    )
