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

**#4570 conversion B note**: this file used to construct its failure cases
via a manifest-declared ``entries`` list naming a file that doesn't exist
on disk (the ``capabilities`` field's own subset-selection feature,
dropped entirely by #4570 conversion B — see ``plugins/manifest.py``).
Registration is now discover-everything (glob ``pipelines/*.yaml`` /
every ``skills/*`` directory) — a NONEXISTENT file is simply never
discovered at all, so "declared but missing" is no longer a reachable
shape. The witnesses below instead use a file/dir that EXISTS on disk
(so the discover-glob finds it) but is independently broken in a way its
OWN sub-install rejects — a malformed pipeline DSL file, and a directory
with no ``SKILL.md`` inside it — reaching the identical ``status="error"``
return-without-raising path #4590 is actually about.

Real ``OpContext``/``PermissionResolver``/``EventLog``/``Workspace``
throughout — no hand-rolled stand-in classes (CLAUDE.md: "NEVER fake a
collaborator ... when a real instance is cheaply constructible"; both are
cheaply constructible, per #4581/#4587's own real-``EventLog`` and
#4581's real-``Workspace`` usage the same night). Skip witnessing only
covers the ``"error"`` sub-status path (a malformed DSL / missing
SKILL.md, both sub-installs' own most common failure); the ``"blocked"``
(threat-scan) path isn't separately exercised here — it's already covered
end-to-end by ``pipeline_install.py``/``skill_install.py``'s own
threat-scan tests, and this file only needs ONE non-``"installed"``
status to prove the routing reads ``sub_result["status"]`` rather than
assuming success.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.core.op_runtime.plugin_install import plugins_root
from reyn.data.workspace.workspace import Workspace
from reyn.plugins.manifest import PLUGIN_MANIFEST_SCHEMA_URL
from reyn.schemas.models import PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import settle


def _make_valid_pipeline_dsl(path: Path, *, name: str = "hello") -> None:
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


def _make_malformed_pipeline_dsl(path: Path) -> None:
    """A real ``*.yaml`` file the discover-glob WILL find — its content is
    what makes ``_parse_pipeline_file`` fail (``PipelineParseError``),
    reaching the same ``status="error"`` return-without-raising path a
    declared-but-missing entry used to (#4570 conversion B: discover-
    everything can't construct "missing" any more — see module
    docstring)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pipeline: broken\nsteps: \"not-a-list-of-steps\"\n", encoding="utf-8")


def _make_valid_skill(path: Path, *, name: str = "hello") -> None:
    """A minimal VALID SKILL.md (same shape as test_plugin_install.py's
    own accept-path fixture)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )


def _make_plugin(base: Path, *, name: str = "myplugin") -> Path:
    """A minimal plugin dir: manifest only. #4570 conversion B: no
    ``capabilities``/``entries`` — registration discovers ``pipelines/``
    and ``skills/`` purely from what the caller separately creates on
    disk under this returned root."""
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "$schema": PLUGIN_MANIFEST_SCHEMA_URL,
            "name": name, "version": "0.1.0", "description": "test plugin",
        }),
        encoding="utf-8",
    )
    return plugin_dir


def _ctx(tmp_path: Path, events: EventLog) -> OpContext:
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
        workspace=Workspace(events=events, permission_resolver=resolver, base_dir=project_root),
        events=events,
        permission_decl=PermissionDecl(
            file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
        ),
        permission_resolver=resolver,
        actor="test",
    )


@pytest.mark.asyncio
async def test_a_malformed_pipeline_dsl_lands_in_skipped_not_registered(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the core witness — a discovered ``pipelines/*.yaml`` file
    whose content fails to parse errors inside ``_pipeline_install_handle``
    (returns ``status="error"``, doesn't raise) and must NOT appear in
    ``registered["pipelines"]``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(tmp_path / "src")
    _make_malformed_pipeline_dsl(src / "pipelines" / "broken.yaml")
    events = EventLog()
    calls: list = []
    events.add_subscriber(lambda e: calls.append(e))
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)
    await settle(events)

    assert result["status"] == "installed", result
    assert result["registered"]["pipelines"] == []
    assert [s["status"] for s in result["skipped"]["pipelines"]] == ["error"]

    skip_paths = {
        e.data["path"] for e in calls if e.type == "pipeline_install_skipped"
    }
    assert skip_paths == {str(Path(result["plugin_root"]) / "pipelines" / "broken.yaml")}
    skip_reasons = {
        e.data["reason"] for e in calls if e.type == "pipeline_install_skipped"
    }
    assert skip_reasons == {"error"}


@pytest.mark.asyncio
async def test_a_skill_dir_with_no_skill_md_lands_in_skipped_not_registered(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: same witness, the skills axis — a discovered ``skills/*``
    directory (the discover-glob only needs ``is_dir()``, #4570 conversion
    B) with no ``SKILL.md`` inside fails ``skill_install``'s own resolve
    step."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(tmp_path / "src")
    (src / "skills" / "empty_skill").mkdir(parents=True)
    events = EventLog()
    calls: list = []
    events.add_subscriber(lambda e: calls.append(e))
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)
    await settle(events)

    assert result["status"] == "installed", result
    assert result["registered"]["skills"] == []
    assert [s["status"] for s in result["skipped"]["skills"]] == ["error"]

    skip_paths = {
        e.data["path"] for e in calls if e.type == "skill_install_skipped"
    }
    assert skip_paths == {str(Path(result["plugin_root"]) / "skills" / "empty_skill")}


@pytest.mark.asyncio
async def test_one_good_one_bad_pipeline_partitions_correctly(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the realistic shape #4590 reports — a plugin shipping
    MULTIPLE pipeline files where only some fail. Proves the fix operates
    per-file (partial success), not all-or-nothing: the valid file must
    land in ``registered``, the invalid one in ``skipped`` — NEITHER list
    may absorb the other's member."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(tmp_path / "src")
    _make_valid_pipeline_dsl(src / "pipelines" / "good.yaml", name="good-pipeline")
    _make_malformed_pipeline_dsl(src / "pipelines" / "broken.yaml")

    events = EventLog()
    calls: list = []
    events.add_subscriber(lambda e: calls.append(e))
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    # Identity, not just count: the GOOD file landed in registered, the
    # BAD one in skipped — neither list absorbed the other's member.
    registered_names = [
        n for r in result["registered"]["pipelines"] for n in r.get("registered_names", [])
    ]
    assert any("good-pipeline" in n for n in registered_names)
    skipped_paths = {s.get("path") for s in result["skipped"]["pipelines"]}
    assert skipped_paths == {str(Path(result["plugin_root"]) / "pipelines" / "broken.yaml")}


@pytest.mark.asyncio
async def test_a_working_skill_still_registers_normally(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: accept-side — a plugin shipping only valid content sees no
    regression: everything lands in ``registered``, ``skipped`` stays
    empty, no skip event fires."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = _make_plugin(tmp_path / "src")
    _make_valid_skill(src / "skills" / "hello", name="hello")

    events = EventLog()
    calls: list = []
    events.add_subscriber(lambda e: calls.append(e))
    ctx = _ctx(tmp_path, events)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)
    await settle(events)

    assert result["status"] == "installed", result
    assert [r["status"] for r in result["registered"]["skills"]] == ["installed"]
    assert result["skipped"]["skills"] == []
    assert not [e for e in calls if e.type == "skill_install_skipped"]
