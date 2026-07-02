"""Tier 2: mcp__install_local schedules a hot-reload after writing config.

Before this fix, _handle_mcp_install_local bypassed mcp_install_handle and
wrote .reyn/mcp.yaml directly without calling request_reload. An installed
local server never appeared in the same session's list_mcp_servers (required
a restart). The other verbs (mcp__install_registry / mcp__install_package)
route through mcp_install_handle which calls request_reload — local is now
aligned.

Falsify: removing the request_reload call makes test_local_install_schedules_reload
fail (pending stays False) while no-reloader and entry-shape tests remain green.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class _FakeReloader:
    """Minimal HotReloader stand-in — tracks request_reload calls."""

    def __init__(self) -> None:
        self.pending = False
        self.sources: list[str] = []

    def request_reload(self, *, source: str) -> None:
        self.pending = True
        self.sources.append(source)


class _FakePermissionResolver:
    async def require_file_write(self, decl: Any, path: str, caller: str) -> None:
        pass


class _FakeCtx:
    router_state = None
    permission_resolver = _FakePermissionResolver()

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    @property
    def workspace(self) -> Any:
        class _W:
            pass
        w = _W()
        w.root = str(self._root)
        return w


def _run_install(
    reloader: _FakeReloader | None,
    project_root: Path,
    name: str = "local-test",
    command: str = "python",
    args: list[str] | None = None,
) -> dict:
    """Run _handle_mcp_install_local with real disk I/O against project_root."""
    import reyn.runtime.hot_reload as hr_mod
    from reyn.tools.mcp_verbs import _handle_mcp_install_local

    orig = hr_mod._active_hot_reloader
    try:
        hr_mod._active_hot_reloader = reloader
        return asyncio.run(
            _handle_mcp_install_local(
                {"name": name, "command": command, "args": args or ["/tmp/server.py"]},
                _FakeCtx(project_root),
            )
        )
    finally:
        hr_mod._active_hot_reloader = orig


def test_local_install_schedules_reload(tmp_path: Path) -> None:
    """Tier 2: mcp__install_local calls request_reload — installed server visible next turn."""
    reloader = _FakeReloader()
    result = _run_install(reloader, tmp_path)
    assert result["status"] == "ok", f"install failed: {result}"
    assert reloader.pending is True, "request_reload must fire — server won't appear without reload"
    assert "mcp__install_local" in reloader.sources


def test_local_install_no_reload_when_no_active_reloader(tmp_path: Path) -> None:
    """Tier 2: no active reloader (CLI / subprocess) → success, not a crash."""
    result = _run_install(reloader=None, project_root=tmp_path)
    assert result["status"] == "ok"


def test_local_install_returns_entry_shape(tmp_path: Path) -> None:
    """Tier 2: result carries the registered entry so callers can confirm what was written."""
    reloader = _FakeReloader()
    result = _run_install(reloader, tmp_path)
    data = result["data"]
    assert data["kind"] == "mcp_install_local"
    assert data["name"] == "local-test"
    assert "entry" in data
    assert data["entry"]["command"] == "python"


def test_local_install_writes_config_to_disk(tmp_path: Path) -> None:
    """Tier 2: the server entry actually lands in .reyn/config/mcp.yaml."""
    from reyn.core.op_runtime.mcp_install import _read_yaml_config, _scope_to_path

    reloader = _FakeReloader()
    _run_install(reloader, tmp_path, name="my-server", command="node")
    config_path = _scope_to_path("local", tmp_path)
    data = _read_yaml_config(config_path)
    servers = data.get("mcp", {}).get("servers", {})
    assert "my-server" in servers, f"server not in config: {servers}"
    assert servers["my-server"]["command"] == "node"
