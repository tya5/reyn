"""Tier 2: #1401 — `reyn web` scoped capabilities reach the A2A server path.

PR-A ports the 3 scoped capabilities `reyn chat` has (#1289/#1398/#1400) to the
A2A/`reyn web` path so a headless SWE-eval server runs agent file/exec ops in a
per-instance container, scopes out tools (web for faithful eval), and grants
repo file-write. The env-backend INSTANCE is threaded (not an env-var string —
rebuilding it app-side would double-build the container, re-opening the drift
class #1402/#1412 rooted) via web/deps' module-global holder.

Observable invariants (no private-state asserts):

- the CLI override holder is contextmanager-isolated (set/reset, incl. the
  cached perm-resolver / registry singletons) — no cross-test leak;
- a plain `reyn web` (no scoped flag) is a no-op (byte-identical pre-#1401);
- each scoped flag + `--reload` fails loud (the module-global can't cross
  uvicorn's reload-worker subprocess — silent-no-op footgun);
- the holder's values round-trip to the construction: web/deps' factory passes
  them to `build_scoped_chat_session` (the #1402-documented None-default fill),
  and the perm resolver applies the grant + file_zone anchor.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

from reyn.cli.commands.web import _apply_cli_scoped_overrides
from reyn.permissions.permissions import PermissionDecl
from reyn.sandbox.policy import SandboxPolicy
from reyn.web import deps
from reyn.web.deps import (
    CliScopedOverrides,
    cli_scoped_overrides,
    get_cli_scoped_overrides,
    set_cli_scoped_overrides,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        env_backend="host", container=None, repo_dir=None,
        grant_file_write=False, exclude_tools=None, reload=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Holder + contextmanager isolation
# ---------------------------------------------------------------------------


def test_cli_scoped_overrides_contextmanager_isolation():
    """Tier 2: #1401 — the contextmanager applies overrides for the block then
    restores (incl. the cached perm-resolver/registry singletons), so the
    override never leaks across tests."""
    set_cli_scoped_overrides(None)
    assert get_cli_scoped_overrides().exclude_tools is None
    ov = CliScopedOverrides(exclude_tools=frozenset({"web__search"}), grant_file_write=True)
    with cli_scoped_overrides(ov):
        g = get_cli_scoped_overrides()
        assert g.exclude_tools == frozenset({"web__search"})
        assert g.grant_file_write is True
    assert get_cli_scoped_overrides().exclude_tools is None
    assert get_cli_scoped_overrides().grant_file_write is False


# ---------------------------------------------------------------------------
# CLI helper behaviour (no-op / --reload guard / holder threading)
# ---------------------------------------------------------------------------


def test_no_scoped_flag_is_noop():
    """Tier 2: #1401 — a plain `reyn web` (host backend, no flags) sets no
    override = byte-identical pre-#1401 behaviour."""
    set_cli_scoped_overrides(None)
    _apply_cli_scoped_overrides(_ns())
    assert get_cli_scoped_overrides() == CliScopedOverrides()


@pytest.mark.parametrize("scoped_kw", [
    {"env_backend": "docker"},
    {"grant_file_write": True},
    {"exclude_tools": "web__search,web__fetch"},
])
def test_reload_guard_blocks_each_scoped_flag(scoped_kw):
    """Tier 2: #1401 — each scoped flag + --reload fails loud (SystemExit), not a
    silent no-op. The guard fires BEFORE build_environment_backend so no
    container is launched by this test."""
    set_cli_scoped_overrides(None)
    with pytest.raises(SystemExit):
        _apply_cli_scoped_overrides(_ns(reload=True, **scoped_kw))


def test_exclude_and_grant_thread_to_holder():
    """Tier 2: #1401 — --exclude-tools (comma-split) + --grant-file-write thread
    to the holder for the factory / resolver to read. No container launch on the
    exclude/grant-only path (env_backend stays None)."""
    set_cli_scoped_overrides(None)
    try:
        _apply_cli_scoped_overrides(
            _ns(exclude_tools="web__search, web__fetch", grant_file_write=True)
        )
        g = get_cli_scoped_overrides()
        assert g.exclude_tools == frozenset({"web__search", "web__fetch"})
        assert g.grant_file_write is True
        assert g.environment_backend is None
    finally:
        set_cli_scoped_overrides(None)


# ---------------------------------------------------------------------------
# Round-trip wiring (the holder's values reach the construction)
# ---------------------------------------------------------------------------


def _deps_tree() -> ast.AST:
    return ast.parse((_SRC / "web" / "deps.py").read_text(encoding="utf-8"))


def _scoped_attr(value: ast.AST | None, attr: str) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == attr
        and isinstance(value.value, ast.Name)
        and value.value.id == "_scoped"
    )


def test_factory_threads_holder_to_build_scoped_chat_session():
    """Tier 2: #1401 — web/deps' _session_factory passes the holder's scoped
    fields (NOT None) to build_scoped_chat_session — the round-trip fill of the
    #1402-documented gaps. The env-backend instance feeds BOTH FS+exec seams
    (single-shared sandbox #1200). Falsifiable: revert any to None → fails."""
    calls = [
        n for n in ast.walk(_deps_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_scoped_chat_session"
    ]
    assert calls, "no build_scoped_chat_session call in web/deps.py"
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    assert _scoped_attr(kw.get("exclude_tools"), "exclude_tools")
    assert _scoped_attr(kw.get("environment_backend"), "environment_backend")
    assert _scoped_attr(kw.get("sandbox_backend"), "environment_backend")
    assert _scoped_attr(kw.get("workspace_base_dir"), "workspace_base_dir")
    assert _scoped_attr(kw.get("workspace_state_dir"), "workspace_state_dir")


# ---------------------------------------------------------------------------
# Real-resolver enforcement (R1): the holder grant + file_zone actually GATE
# ---------------------------------------------------------------------------


async def _web_can_write(target: str, sandbox: "SandboxPolicy", *, grant: bool, ws) -> bool:
    """Build the REAL web `_get_perm_resolver()` under the holder and check
    whether ``require_file_write`` permits ``target`` (no None resolver)."""
    with cli_scoped_overrides(CliScopedOverrides(grant_file_write=grant, workspace_base_dir=ws)):
        resolver = deps._get_perm_resolver()
        try:
            await resolver.require_file_write(PermissionDecl(), target, "default", sandbox_policy=sandbox)
            return True
        except PermissionError:
            return False


@pytest.mark.asyncio
async def test_grant_file_write_gates_real_in_repo_write(tmp_path, monkeypatch):
    """Tier 2: R1 — the --grant-file-write holder flag ACTUALLY gates file.write
    on the REAL web perm resolver. Differential (the falsification pair):
    grant=True allows an in-repo write, grant=False denies it. Not a string-grep."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "container_repo"
    sandbox = SandboxPolicy(write_paths=[str(repo)])
    target = str(repo / "src" / "x.py")
    assert await _web_can_write(target, sandbox, grant=True, ws=repo) is True
    assert await _web_can_write(target, sandbox, grant=False, ws=repo) is False


@pytest.mark.asyncio
async def test_file_zone_root_from_holder_anchors_default_zone(tmp_path, monkeypatch):
    """Tier 2: R1 — file_zone_root receives the holder's container repo root
    (#1414) at RUNTIME. Differential (no grant): a write into <repo>/.reyn (the
    default write zone anchored on file_zone_root) is allowed when ws_base_dir=
    repo, but DENIED when the holder has no ws_base_dir (zone anchors on host)."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "container_repo"
    sandbox = SandboxPolicy(write_paths=[str(repo)])
    default_zone_target = str(repo / ".reyn" / "x.yaml")
    assert await _web_can_write(default_zone_target, sandbox, grant=False, ws=repo) is True
    assert await _web_can_write(default_zone_target, sandbox, grant=False, ws=None) is False
