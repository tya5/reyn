"""Tier 2: #4590 — a failed pipeline/skill sub-install landed in
``plugin_install``'s ``registered`` list, unconditionally.

#4580 added a ``skipped`` key alongside ``registered`` (per capability
kind) for the DECLARED-but-not-registered case — but only wired it for
``mcp`` (a probe-then-commit axis that skips BEFORE the sub-install ever
runs). Pipelines/skills have a DIFFERENT drop path: their own sub-install
call (``pipeline_install``/``skill_install``) always runs and can itself
return ``{"status": "error"/"blocked", ...}`` instead of raising — before
this fix, that return value was appended to ``registered`` regardless of
its own ``status``, so a failed pipeline/skill read as registered, and
``skipped["pipelines"]``/``skipped["skills"]`` stayed empty no matter how
many actually failed. Worse than #4580's own silent drop: #4580 said
nothing, this said something false.

Real ``OpContext``/``PermissionResolver`` throughout (mirrors
``test_plugin_install.py``'s own ``_make_ctx`` construction, which already
approves the pipelines.yaml/skills.yaml write gates a real accept-path
sub-install needs). No mocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.core.op_runtime.plugin_install import plugins_root
from reyn.schemas.models import PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


class _Events:
    """Minimal real-callable event log — records emitted calls for
    audit-event witnessing (same pattern as test_plugin_install.py's own
    ``_Events`` stub)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.calls.append((kind, kwargs))


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _make_pipeline_dsl(path: Path, *, name: str = "hello") -> None:
    """A minimal VALID pipeline DSL file (same shape as
    test_pipeline_install.py's own ``_make_pipeline_dsl``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"pipeline: {name}\n"
        "description: A test pipeline\n"
        "steps:\n"
        "  - transform: {value: \"1 + 1\", output: two}\n",
        encoding="utf-8",
    )


def _make_skill(path: Path, *, name: str = "hello") -> None:
    """A minimal VALID SKILL.md (same shape as test_plugin_install.py's
    own accept-path fixture)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )


def _make_plugin(
    base: Path, *, name: str = "myplugin",
    pipeline_entries: list[str], skill_entries: list[str],
) -> Path:
    """A plugin declaring explicit ``entries`` for both capabilities (so a
    caller controls exactly which names are declared, valid or not — the
    manifest's own ``entries`` list is the "declared" side of the
    declared-vs-registered diff this file tests)."""
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "test plugin",
            "capabilities": [
                {"kind": "pipelines", "entries": pipeline_entries},
                {"kind": "skills", "entries": skill_entries},
            ],
        }),
        encoding="utf-8",
    )
    return plugin_dir


def _ctx(tmp_path: Path, events: "_Events") -> OpContext:
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    for cfg in ("pipelines.yaml", "skills.yaml"):
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
        )
    return OpContext(
        workspace=_StubWorkspace(base_dir=project_root),
        events=events,
        permission_decl=PermissionDecl(
            file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
        ),
        permission_resolver=resolver,
        actor="test",
    )


@pytest.mark.asyncio
async def test_a_missing_pipeline_dsl_file_lands_in_skipped_not_registered(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the core witness — a declared pipeline entry naming a file
    that doesn't exist under ``pipelines/`` fails inside
    ``_pipeline_install_handle`` (returns ``status="error"``, doesn't
    raise) and must NOT appear in ``registered["pipelines"]``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(
        tmp_path / "src", pipeline_entries=["missing.yaml"], skill_entries=[],
    )
    events = _Events()
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    assert result["registered"]["pipelines"] == []
    assert [s["status"] for s in result["skipped"]["pipelines"]] == ["error"]

    skip_paths = {
        c[1]["path"] for c in events.calls if c[0] == "pipeline_install_skipped"
    }
    assert skip_paths == {str(Path(result["plugin_root"]) / "pipelines" / "missing.yaml")}
    skip_reasons = {
        c[1]["reason"] for c in events.calls if c[0] == "pipeline_install_skipped"
    }
    assert skip_reasons == {"error"}


@pytest.mark.asyncio
async def test_a_missing_skill_dir_lands_in_skipped_not_registered(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: same witness, the skills axis."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(
        tmp_path / "src", pipeline_entries=[], skill_entries=["missing_skill"],
    )
    events = _Events()
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    assert result["registered"]["skills"] == []
    assert [s["status"] for s in result["skipped"]["skills"]] == ["error"]

    skip_paths = {
        c[1]["path"] for c in events.calls if c[0] == "skill_install_skipped"
    }
    assert skip_paths == {str(Path(result["plugin_root"]) / "skills" / "missing_skill")}


@pytest.mark.asyncio
async def test_one_good_one_bad_pipeline_entry_partitions_correctly(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the realistic shape #4590 reports — a plugin declaring
    MULTIPLE pipeline entries where only some fail. Proves the fix
    operates per-entry (partial success), not all-or-nothing: the valid
    entry must land in ``registered``, the invalid one in ``skipped`` —
    NEITHER list may absorb the other's member."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(
        tmp_path / "src",
        pipeline_entries=["good.yaml", "missing.yaml"], skill_entries=[],
    )
    _make_pipeline_dsl(src / "pipelines" / "good.yaml", name="good-pipeline")

    events = _Events()
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    # Identity, not just count: the GOOD entry landed in registered, the
    # BAD one in skipped — neither list absorbed the other's member.
    registered_names = [
        n for r in result["registered"]["pipelines"] for n in r.get("registered_names", [])
    ]
    assert any("good-pipeline" in n for n in registered_names)
    skipped_paths = {s.get("path") for s in result["skipped"]["pipelines"]}
    assert skipped_paths == {str(Path(result["plugin_root"]) / "pipelines" / "missing.yaml")}


@pytest.mark.asyncio
async def test_a_working_skill_still_registers_normally(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: accept-side — a plugin whose entries all succeed sees no
    regression: everything lands in ``registered``, ``skipped`` stays
    empty, no skip event fires. (test_plugin_install.py's own accept-path
    test already covers this for the discover-everything, no-explicit-
    entries case; this covers the explicit-entries path this file's other
    tests exercise, for symmetry.)"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(
        tmp_path / "src", pipeline_entries=[], skill_entries=["hello"],
    )
    _make_skill(src / "skills" / "hello", name="hello")

    events = _Events()
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    assert [r["status"] for r in result["registered"]["skills"]] == ["installed"]
    assert result["skipped"]["skills"] == []
    assert not [c for c in events.calls if c[0] == "skill_install_skipped"]
