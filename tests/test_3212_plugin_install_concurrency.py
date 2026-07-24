"""Tier 2: OS invariant — #3212 plugin install/uninstall concurrency fix.

#3212 root cause: ``reconcile_plugin_installs`` (step 0 of ``plugin_install``)
could not distinguish a genuinely-crashed partial install (an
``_install_state.json`` marker left behind by a dead process) from a
CONCURRENT still-in-progress install of the SAME name (marker present, owner
very much alive) — both looked identical, so a concurrent reconcile could
``rmtree`` a live install mid-copy (the reported symptom: ``rag_ingest``/
``rag_query`` FAILED "No such file or directory" under concurrent
install/uninstall cycles). ``~/.reyn/plugins/`` stays a DELIBERATE single
global path (ADR 0064 §3.3 "install once, use everywhere") — this is a pure
concurrency-correctness fix, no scope change.

Tests:
  1. THE RACE (primary #3212 symptom): a marker with a LIVE pid (the current
     process) is NOT wiped by reconcile — a concurrent in-flight install is
     respected, not treated as crashed.
  2. Crash-partial rollback (recovery correctness): a marker with a DEAD pid
     IS rolled back — reconcile can still tell a genuine crash from a live
     install; #1 and #2 together are the discriminator #3212 requires.
  3. Atomic rename: the copy is staged into a temp dir under
     ``~/.reyn/plugins/.staging/`` and only appears at the final
     ``plugin_root`` via an atomic ``Path.replace`` — a reader never
     observes a partially-copied tree at the final path, and no staging
     leftovers survive a successful install.
  4. Lock serialization: ``plugin_name_lock`` (the same per-name lock
     ``plugin_install``/``plugin_uninstall`` take around their mutating
     steps) blocks a second acquirer of the same name and raises
     ``TimeoutError`` on a bounded wait rather than allowing the two to
     interleave. Tested via the lock primitive directly (deterministic, no
     sleep-races).
  5. Legacy marker (no pid field): a pre-#3212 marker with no ``pid`` key is
     treated as crashed (rolled back) — matches ``reconcile_plugin_installs``'
     documented "dead / missing / legacy marker -> roll back" contract.

Real filesystem (HOME monkeypatched to a tmp dir) throughout — no mocks.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from reyn.core.op_runtime import plugin_install
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import (
    _install_state_path,
    _write_install_state,
    plugin_name_lock,
    plugins_root,
    reconcile_plugin_installs,
)
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.schemas.models import PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# A PID very unlikely to be allocated on any kernel (mirrors the existing
# precedent in tests/test_action_embedding_index_concurrency.py).
_DEAD_PID = 2**31 - 1


class _StubWorkspace:
    def __init__(self, base_dir) -> None:
        self.base_dir = base_dir


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def _make_ctx(tmp_path):
    """A real OpContext + PermissionResolver, session-approving the global
    plugin-copy write + registry writes — same shape as test_plugin_install.py's
    ``_make_ctx`` (approve-everything baseline; this file is not testing the
    permission gates, those already have dedicated coverage)."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    resolver = PermissionResolver(config_permissions={}, project_root=project_root, interactive=False)
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    for cfg in ("pipelines.yaml", "skills.yaml", "mcp.yaml"):
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


def _make_plugin_source(base, name: str = "concplugin"):
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "concurrency test plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: says hi\n---\n\nBody.\n", encoding="utf-8",
    )
    return plugin_dir


# ── Test 1: the race — a LIVE marker is NOT wiped ────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_skips_live_concurrent_install(tmp_path, monkeypatch):
    """Tier 2: #3212 THE RACE — a marker whose pid is the CURRENT (alive)
    process models a concurrent, still-in-progress install of this name.
    reconcile must NOT wipe it. RED (pre-#3212 behavior) if this entry is
    rolled back — that is the exact symptom: a concurrent reconcile wiping a
    live install's in-flight copy mid-run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    in_flight = plugins_root() / "in-flight-plugin"
    (in_flight / "skills" / "s").mkdir(parents=True)
    _write_install_state(in_flight, "local", pid=os.getpid())

    rolled_back = await reconcile_plugin_installs(plugins_root())

    assert rolled_back == [], (
        "reconcile wiped a marker with a LIVE pid — the exact #3212 race "
        "(a concurrent in-flight install looked identical to a crashed one)"
    )
    assert in_flight.exists(), "the live in-flight install's copy was deleted"
    assert _install_state_path(in_flight).exists(), "the live marker was removed"


# ── Test 2: crash-partial rollback — a DEAD marker IS wiped ──────────────────


@pytest.mark.asyncio
async def test_reconcile_rolls_back_dead_pid_partial(tmp_path, monkeypatch):
    """Tier 2: recovery-reconstruction correctness witness — a marker with a
    DEAD pid (crashed process) is still rolled back, same as pre-#3212. This
    is the discriminator: #1 (live -> keep) and #2 (dead -> roll back) prove
    reconcile can tell the two apart, which is the whole #3212 fix."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert not plugin_install.pid_alive(_DEAD_PID), (
        f"precondition: PID {_DEAD_PID} must not be alive for this test"
    )

    crashed = plugins_root() / "crashed-plugin"
    (crashed / "skills" / "s").mkdir(parents=True)
    _write_install_state(crashed, "local", pid=_DEAD_PID)

    rolled_back = await reconcile_plugin_installs(plugins_root())

    assert rolled_back == ["crashed-plugin"]
    assert not crashed.exists(), "a dead-pid crashed partial was not rolled back"


# ── Test 3: atomic rename copy ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_copy_is_staged_then_atomically_renamed(tmp_path, monkeypatch):
    """Tier 2: #3212 layer c — the copy is built in a unique staging dir
    under ``.staging/`` and only appears at the final ``plugin_root`` via an
    atomic ``Path.replace``: while ``_copy_plugin_tree`` is writing, the
    FINAL destination does not exist yet (nothing for a concurrent reader to
    observe half-formed); after a successful install, no staging leftovers
    remain. RED if the copy writes directly into ``plugin_root`` (pre-#3212:
    a concurrent reader could see a partially-copied tree)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src")
    ctx = _make_ctx(tmp_path)

    final_root = plugins_root() / "concplugin"
    observed_dest_paths: list = []
    real_copy = plugin_install._copy_plugin_tree

    def _spy_copy(src_dir, dest_dir):
        observed_dest_paths.append(dest_dir)
        # While the copy is happening, the FINAL path must not exist yet —
        # the tree is being built in staging, not in place.
        assert not final_root.exists(), (
            "plugin_root exists WHILE the copy is still in progress — the "
            "copy is not staged, a concurrent reader could see a partial tree"
        )
        real_copy(src_dir, dest_dir)

    monkeypatch.setattr(plugin_install, "_copy_plugin_tree", _spy_copy)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    assert observed_dest_paths, "the copy spy was never invoked"
    assert observed_dest_paths[0] != final_root, "copy wrote directly to plugin_root, not a staging dir"
    assert observed_dest_paths[0].parent.name == ".staging"

    assert final_root.is_dir(), "the final plugin_root was never populated (rename missing)"
    assert (final_root / "skills" / "hello" / "SKILL.md").exists()

    staging_root = plugins_root() / ".staging"
    remaining = [p for p in staging_root.iterdir()] if staging_root.is_dir() else []
    assert remaining == [], f"staging leftovers survived a successful install: {remaining}"


# ── Test 4: lock serialization (deterministic — the primitive directly) ──────


@pytest.mark.asyncio
async def test_plugin_name_lock_blocks_and_times_out(tmp_path):
    """Tier 2: #3212 layer b — ``plugin_name_lock`` serializes mutations on
    the same plugin name: while one holder has the lock, a second acquirer
    for the SAME name times out (``TimeoutError``) rather than proceeding
    concurrently (which is exactly what would let an install's copytree
    interleave with an uninstall's rmtree). Uses the lock primitive directly
    (no real subprocess/thread races needed — deterministic)."""
    root = tmp_path / "plugins"

    async with plugin_name_lock("shared-name", root, timeout=1.0):
        with pytest.raises(TimeoutError):
            async with plugin_name_lock("shared-name", root, timeout=0.2):
                pytest.fail("should never enter — the outer lock is still held")

    # A DIFFERENT name is not blocked by the first name's lock.
    async with plugin_name_lock("other-name", root, timeout=1.0):
        pass

    # After the outer lock above is released, the SAME name is acquirable again.
    async with plugin_name_lock("shared-name", root, timeout=1.0):
        pass


# ── Test 5: legacy marker (no pid field) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_treats_legacy_no_pid_marker_as_crashed(tmp_path, monkeypatch):
    """Tier 2: a pre-#3212 marker with no ``pid`` key at all (written by an
    older reyn version, or corrupted) is treated as crashed and rolled back —
    matches ``reconcile_plugin_installs``' documented contract ("dead /
    missing / legacy marker -> roll back as today"). Err-safe direction is
    KEEP-on-live-pid (test 1); a marker that carries no liveness signal at
    all falls back to the pre-#3212 default of rolling it back."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    legacy = plugins_root() / "legacy-plugin"
    (legacy / "skills" / "s").mkdir(parents=True)
    state_path = _install_state_path(legacy)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"name": "legacy-plugin", "kind": "local", "status": "installing"}),
        encoding="utf-8",
    )
    assert "pid" not in json.loads(state_path.read_text(encoding="utf-8"))

    rolled_back = await reconcile_plugin_installs(plugins_root())

    assert rolled_back == ["legacy-plugin"]
    assert not legacy.exists(), "a legacy no-pid marker was not treated as crashed"
