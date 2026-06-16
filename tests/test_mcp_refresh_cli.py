"""Tier 2: reyn mcp refresh CLI — FP-0037 S1.

Pins the contract for the `refresh` subcommand:
  - Argument parsing (positional args, --project flag)
  - Writes the cache file with the correct shape on success
  - Handles zero configured servers gracefully
  - Per-server failure → warning printed, empty list in cache, exit 0
  - Atomic write (no stale .tmp lingering)

Uses monkeypatch.setattr on the module-level _probe_server_tools helper
(= the extracted probe callable) so tests don't spin up real MCP servers.
No unittest.mock / AsyncMock / MagicMock.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from reyn.chat.services.mcp_cache_file import cache_file_path, read_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mcp(argv: list[str]):
    """Parse argv via the real mcp.register argparse tree."""
    import argparse

    from reyn.interfaces.cli.commands.mcp import register

    root = argparse.ArgumentParser(prog="reyn")
    sub = root.add_subparsers(dest="command")
    register(sub)
    return root.parse_args(argv)


# ---------------------------------------------------------------------------
# 1. Argument parsing
# ---------------------------------------------------------------------------


def test_refresh_parses(tmp_path: Path) -> None:
    """Tier 2: 'mcp refresh' parses without error; --project PATH is accepted."""
    ns = _parse_mcp(["mcp", "refresh"])
    assert ns.mcp_command == "refresh"
    assert ns.project is None

    ns2 = _parse_mcp(["mcp", "refresh", "--project", str(tmp_path)])
    assert ns2.project == str(tmp_path)


# ---------------------------------------------------------------------------
# Fake probe helper (= replaces _probe_server_tools for tests)
# ---------------------------------------------------------------------------


def _make_fake_probe(tools_by_server: dict[str, list[dict]]):
    """Return an async _probe_server_tools replacement returning fixed tools."""

    async def _fake(server_name: str, cfg: dict, *, per_server_timeout: float = 5.0):
        return server_name, tools_by_server.get(server_name, [])

    return _fake


# ---------------------------------------------------------------------------
# 2. Writes cache file with correct shape
# ---------------------------------------------------------------------------


def test_refresh_writes_cache_file(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with a stub probe, run_refresh writes mcp_tools_cache.json
    containing the expected servers dict."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    fixed_tools = [{"name": "get_repo", "description": "Get a repository"}]
    monkeypatch.setattr(
        mcp_cmd,
        "_probe_server_tools",
        _make_fake_probe({"myserver": fixed_tools}),
    )

    # Build a fake project root with a reyn.yaml that has one MCP server.
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text(
        "mcp:\n  servers:\n    myserver:\n      type: stdio\n      command: fake\n",
        encoding="utf-8",
    )

    # Patch _all_servers_with_scope to return our server without needing
    # _find_project_root to walk the filesystem.
    monkeypatch.setattr(
        mcp_cmd,
        "_all_servers_with_scope",
        lambda root: [("myserver", "project", {"type": "stdio", "command": "fake"})],
    )
    # Also patch _get_project_root so the state_dir lands in tmp_path.
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)

    import argparse
    ns = argparse.Namespace(project=None, func=mcp_cmd.run_refresh)
    mcp_cmd.run_refresh(ns)

    state_dir = project_root / ".reyn" / "state"
    cache_path = cache_file_path(state_dir)
    result = read_cache(cache_path)
    assert result is not None
    assert "myserver" in result
    assert result["myserver"] == fixed_tools


# ---------------------------------------------------------------------------
# 3. Zero configured servers → empty cache written
# ---------------------------------------------------------------------------


def test_refresh_handles_empty_server_config(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with no configured servers, run_refresh writes servers: {}."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "empty_proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("", encoding="utf-8")

    monkeypatch.setattr(mcp_cmd, "_all_servers_with_scope", lambda root: [])
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)

    import argparse
    ns = argparse.Namespace(project=None, func=mcp_cmd.run_refresh)
    mcp_cmd.run_refresh(ns)

    state_dir = project_root / ".reyn" / "state"
    cache_path = cache_file_path(state_dir)
    result = read_cache(cache_path)
    assert result == {}


# ---------------------------------------------------------------------------
# 4. Per-server failure → warning printed, empty list written, exit 0
# ---------------------------------------------------------------------------


def test_refresh_per_server_failure_warns_writes_empty(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Tier 2: a server that times out / errors gets empty list in cache;
    a warning is printed; the command exits without raising."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    async def _failing_probe(server_name, cfg, *, per_server_timeout=5.0):
        return server_name, []  # simulates timeout / connection error

    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", _failing_probe)

    project_root = tmp_path / "proj2"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        mcp_cmd,
        "_all_servers_with_scope",
        lambda root: [("badserver", "project", {"type": "stdio", "command": "fake"})],
    )
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)

    import argparse
    ns = argparse.Namespace(project=None, func=mcp_cmd.run_refresh)
    # Must not raise (= exit 0 equivalent for a function that doesn't call sys.exit).
    mcp_cmd.run_refresh(ns)

    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or "failed" in captured.err.lower(), (
        "expected a warning on stderr for a failed server probe"
    )

    state_dir = project_root / ".reyn" / "state"
    result = read_cache(cache_file_path(state_dir))
    assert result is not None
    assert result.get("badserver") == [], (
        "failed server must be written as empty list, not omitted"
    )


# ---------------------------------------------------------------------------
# 5. Atomic write — no stale .tmp file after success
# ---------------------------------------------------------------------------


def test_refresh_uses_atomic_write(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: after run_refresh completes, no .tmp sibling file lingers."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    monkeypatch.setattr(
        mcp_cmd,
        "_probe_server_tools",
        _make_fake_probe({"s": [{"name": "t", "description": "d"}]}),
    )

    project_root = tmp_path / "proj3"
    project_root.mkdir()

    monkeypatch.setattr(
        mcp_cmd,
        "_all_servers_with_scope",
        lambda root: [("s", "project", {"type": "stdio"})],
    )
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)

    import argparse
    ns = argparse.Namespace(project=None, func=mcp_cmd.run_refresh)
    mcp_cmd.run_refresh(ns)

    state_dir = project_root / ".reyn" / "state"
    cache_path = cache_file_path(state_dir)
    tmp_path_candidate = cache_path.with_suffix(cache_path.suffix + ".tmp")
    assert not tmp_path_candidate.exists(), (
        ".tmp sibling file must not linger after a successful write"
    )
    assert cache_path.exists(), "cache file must exist after successful write"
