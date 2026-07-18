"""Tier 2: OS invariant — ADR 0064 plugin model P2 (plugin_install / plugin_uninstall).

Tests:
  1. e2e install + uninstall roundtrip: a real local plugin dir (skills
     capability) is copied to ~/.reyn/plugins/<name>/, registered into
     .reyn/config/skills.yaml (tagged plugin_id), then plugin_uninstall
     removes both the registry entry and the global copy.
  2. enforcement (real PermissionResolver, no approval): the global-copy
     write is denied — demonstrates the gate is load-bearing, not decorative
     (CLAUDE.md: gate strip-falsify, real resolver not None).
  3. reconcile: a plugin dir left with an _install_state.json marker (a
     simulated crash mid-install) is rolled back by the next reconcile pass.
  4. name-collision precedence (§3.8): a `local` install refuses to shadow
     an already-installed `builtin`-sourced plugin of the same name.

Real PermissionResolver + OpContext throughout (no mocks). HOME is
monkeypatched per-test so ~/.reyn/plugins/ never touches the real home dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import (
    _write_install_state,
    plugins_root,
    reconcile_plugin_installs,
)
from reyn.core.op_runtime.plugin_install import (
    handle as install_handle,
)
from reyn.core.op_runtime.plugin_uninstall import handle as uninstall_handle
from reyn.schemas.models import PluginInstallIROp, PluginUninstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# ── shared stubs (real API surface, no mocks) ─────────────────────────────────


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — passes emit calls through without side effects."""
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _make_plugin_source(base: Path, name: str = "myplugin") -> Path:
    """A minimal local plugin dir: manifest + one skills capability."""
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "test plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    return plugin_dir


def _make_ctx(
    tmp_path: Path, *, approve_plugins_root: bool = True,
) -> OpContext:
    """Build a real OpContext with a PermissionResolver. When
    ``approve_plugins_root`` is True, session-approves ~/.reyn/plugins/
    (recursive) + the three registry config files — the granted-path
    baseline every non-enforcement test needs. The enforcement test passes
    False to demonstrate the gate actually denies without it."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    if approve_plugins_root:
        resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
        for cfg in ("mcp.yaml", "pipelines.yaml", "skills.yaml"):
            resolver.session_approve_path(
                str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
            )

    decl = PermissionDecl(file_write=[{"path": str(plugins_root()), "scope": "recursive"}])
    return OpContext(
        workspace=_StubWorkspace(base_dir=project_root),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
    )


# ── Test 1: e2e install + uninstall roundtrip ─────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_install_uninstall_roundtrip(tmp_path, monkeypatch):
    """Tier 2: a real plugin_install op copies the plugin to ~/.reyn/plugins/<name>/,
    registers its skills capability into .reyn/config/skills.yaml (tagged
    plugin_id), and plugin_uninstall removes both. RED if the copy, the
    registry entry, the plugin_id tag, or the uninstall's removal is missing."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src")
    ctx = _make_ctx(tmp_path)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    assert result["name"] == "myplugin"
    assert result["capabilities"] == ["skills"]

    plugin_root = plugins_root() / "myplugin"
    assert plugin_root.is_dir(), "plugin was not copied to ~/.reyn/plugins/<name>/"
    assert (plugin_root / "skills" / "hello" / "SKILL.md").exists()

    skills_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "skills.yaml"
    raw = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    entry = raw["skills"]["entries"]["hello"]
    assert entry["plugin_id"] == "myplugin", "registered entry missing plugin_id provenance (§3.7)"

    # ── uninstall ──
    uop = PluginUninstallIROp(kind="plugin_uninstall", name="myplugin")
    uresult = await uninstall_handle(uop, ctx)

    assert uresult["status"] == "uninstalled"
    assert uresult["removed"]["skills"] == ["hello"]
    assert uresult["copy_removed"] is True
    assert not plugin_root.exists(), "plugin copy was not removed by uninstall"

    raw_after = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    assert raw_after["skills"]["entries"] == {}, "registry entry survived uninstall"


# ── Test 2: enforcement (real resolver, gate strip-falsify) ──────────────────


@pytest.mark.asyncio
async def test_plugin_install_denied_without_write_approval(tmp_path, monkeypatch):
    """Tier 2: security-critical gate — WITHOUT an approval/JIT-ask grant for
    ~/.reyn/plugins/, a real PermissionResolver denies the global-copy write
    (require_file_write's decl-less "zone OR approved" invariant: a mere
    PermissionDecl declaration does not itself grant). RED if plugin_install
    writes the global copy despite no approval — the exact unauthorized-write
    this gate exists to prevent (ADR 0064 §3.10 item 1)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src", name="unapproved-plugin")
    ctx = _make_ctx(tmp_path, approve_plugins_root=False)

    op = PluginInstallIROp(
        kind="plugin_install", source={"kind": "local", "path": str(source)},
    )
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)

    assert not (plugins_root() / "unapproved-plugin").exists(), (
        "plugin copy was written despite a denied permission gate"
    )


# ── Test 3: reconcile rolls back a crashed partial install ───────────────────


def test_reconcile_plugin_installs_rolls_back_partial_install(tmp_path, monkeypatch):
    """Tier 2: filesystem-consistency reconcile (§3.11) — a plugin dir left
    with an _install_state.json marker (simulating a crash between copy and
    plugin_install_completed) is removed by the next reconcile pass. RED if
    reconcile leaves the partial directory in place (a half-installed plugin
    that is neither usable nor cleanly removable via plugin_uninstall)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    partial = plugins_root() / "crashed-plugin"
    partial.mkdir(parents=True)
    (partial / "some-content.txt").write_text("partial copy", encoding="utf-8")
    _write_install_state(partial, "local")

    completed = plugins_root() / "completed-plugin"
    completed.mkdir(parents=True)
    (completed / "some-content.txt").write_text("completed copy", encoding="utf-8")
    # No _install_state.json marker — this one is NOT touched by reconcile.

    rolled_back = reconcile_plugin_installs(plugins_root())

    assert rolled_back == ["crashed-plugin"]
    assert not partial.exists(), "the crashed partial install was not rolled back"
    assert completed.exists(), "reconcile incorrectly removed a completed install"


# ── Test 4: name-collision precedence (§3.8) ──────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_install_refuses_lower_trust_shadow(tmp_path, monkeypatch):
    """Tier 2: a `local`-sourced install refuses to shadow an already
    -installed `builtin`-sourced plugin of the SAME name (ADR 0064 §3.8: the
    lower-trust-risk source never silently shadows a higher-trust-risk one).
    RED if the local re-install silently overwrites the builtin copy."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Simulate a builtin plugin registered under src/reyn/builtin/plugins/.
    builtin_src_root = tmp_path / "builtin_plugins"
    builtin_source = _make_plugin_source(builtin_src_root, name="shared-name")
    monkeypatch.setattr(
        "reyn.core.op_runtime.plugin_install._builtin_plugin_dir",
        lambda name: builtin_src_root / name,
    )

    ctx = _make_ctx(tmp_path)
    builtin_op = PluginInstallIROp(kind="plugin_install", source={"kind": "builtin", "name": "shared-name"})
    builtin_result = await install_handle(builtin_op, ctx)
    assert builtin_result["status"] == "installed", f"builtin install failed: {builtin_result}"

    local_source = _make_plugin_source(tmp_path / "local_src", name="shared-name")
    local_op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(local_source)})
    local_result = await install_handle(local_op, ctx)

    assert local_result["status"] == "skipped", f"expected skipped, got {local_result}"
    assert "shared-name" in local_result["error"]
