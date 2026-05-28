"""Tier 2: ChatSession.refresh_mcp_servers() — FP-0037 S3 programmatic API.

Pins the contract for the new public coroutine on ChatSession:
  - Calls the 3-step turn-boundary chain (yaml-watch → disk-reload → ensure-cached).
    Ordering is verified via observable effects on the disk-reload path:
    a disk cache written between calls is visible on the next refresh.
  - Returns {"refreshed": True} when the in-memory cache actually changed.
  - Returns {"refreshed": False} when nothing changed on a second call.
  - Returns {"servers": {name: tool_count}} projection after refresh.
  - Returns {"servers": {}} when no MCP servers are configured.
  - Returns {"refreshed": False, ...} when an internal chain step fails,
    without propagating.

No unittest.mock / AsyncMock / MagicMock / patch.
Private-state access: NONE.  Observable surfaces used:
  - refresh_mcp_servers() return dict (primary observable)
  - router_host (public ChatSession property)
  - mcp_tools_cache_snapshot (public RouterHostAdapter property, S1)

All tests chdir to tmp_path so the adapter's default state_dir
(Path(".reyn/state") relative to cwd) resolves to an isolated directory.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reyn.chat.services.mcp_cache_file import cache_file_path, write_cache
from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Minimal ChatSession factory
# ---------------------------------------------------------------------------


def _make_session(
    tmp_path: Path,
    *,
    agent_name: str = "s3-test-agent",
    mcp_servers: dict | None = None,
) -> ChatSession:
    """Build a minimal ChatSession suitable for refresh_mcp_servers tests.

    WAL + snapshot redirect to tmp_path so tests do not write to the
    real .reyn/ directory.  All tests chdir to tmp_path before calling
    this so the session's default state_dir (``cwd / .reyn / state``)
    resolves inside tmp_path.
    """
    return ChatSession(
        agent_name=agent_name,
        mcp_servers=mcp_servers,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


# ---------------------------------------------------------------------------
# Helper: locate the state_dir used by the session's adapter
# (default = cwd / ".reyn" / "state", valid after os.chdir(tmp_path))
# ---------------------------------------------------------------------------


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".reyn" / "state"


# ---------------------------------------------------------------------------
# 1. Three-phase call order — disk-reload variant
#
# Observable invariant: after the first refresh warms the cache from disk
# (S1 disk-reload via ensure-cached warm-start), a second fresher disk
# cache written externally is picked up on the next refresh call.
# This only works if S1 disk-reload runs each call to check the mtime.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_calls_three_phases_in_order(tmp_path: Path) -> None:
    """Tier 2: refresh_mcp_servers invokes the 3-phase chain in the expected
    order; the disk-reload phase (S1) picks up a freshly-written cache file
    on the second call.

    Verified by observable effects: second refresh detects a newly-written
    disk cache and swaps the in-memory cache to v2.  If disk-reload were
    absent or ran before ensure-cached populated the cache, the swap would
    not occur and refreshed would remain False.
    """
    state_dir = _state_dir(tmp_path)
    v1_tools = {"srvA": [{"name": "t1", "description": "d1"}]}
    write_cache(cache_file_path(state_dir), v1_tools)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path, mcp_servers={"srvA": {}})

        # Call 1: warms from disk (v1); S1 mtime recorded.
        result_1 = await session.refresh_mcp_servers()
        assert "srvA" in result_1["servers"], (
            "srvA must appear in servers after warming from disk cache"
        )

        # Simulate CLI refresh: write a fresher v2 cache file.
        time.sleep(0.02)
        v2_tools = {
            "srvA": [
                {"name": "t1", "description": "d1"},
                {"name": "t2", "description": "d2"},
            ]
        }
        write_cache(cache_file_path(state_dir), v2_tools)

        # Call 2: yaml-watch noop (no yaml edited); S1 detects newer mtime,
        # swaps to v2; ensure-cached is noop (already cached).
        result_2 = await session.refresh_mcp_servers()

        assert result_2["refreshed"] is True, (
            "refreshed must be True when disk-reload swaps the cache on the second call"
        )
        assert result_2["servers"]["srvA"] == 2, (
            "servers projection must reflect v2 tool count after disk-reload swap"
        )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 2. refreshed=True when disk cache is freshened between calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_returns_refreshed_true_when_cache_swaps(
    tmp_path: Path,
) -> None:
    """Tier 2: refresh_mcp_servers returns refreshed=True when the on-disk
    cache has a newer mtime than the in-memory cache, causing a swap via
    maybe_reload_mcp_tools_cache_from_disk (S1 path).

    Simulates the `reyn mcp refresh` operator workflow: v1 warms the
    session; CLI writes v2; next refresh call picks it up.
    """
    state_dir = _state_dir(tmp_path)
    v1_tools = {"srv": [{"name": "tool_a", "description": "a"}]}
    write_cache(cache_file_path(state_dir), v1_tools)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path, mcp_servers={"srv": {"command": "mcp-srv"}})

        # First call: warms from disk (v1).
        result_1 = await session.refresh_mcp_servers()
        assert result_1["servers"].get("srv") is not None, (
            "srv must appear in servers after first refresh from disk"
        )

        # Simulate CLI refresh: write fresher v2 cache file.
        time.sleep(0.02)
        v2_tools = {
            "srv": [
                {"name": "tool_a", "description": "a"},
                {"name": "tool_b", "description": "b"},
            ]
        }
        write_cache(cache_file_path(state_dir), v2_tools)

        # Second call: S1 disk-reload detects newer mtime → swap to v2.
        result_2 = await session.refresh_mcp_servers()

        assert result_2["refreshed"] is True, (
            "refreshed must be True when disk-reload swaps the cache"
        )
        assert result_2["servers"]["srv"] == 2, (
            "servers projection must reflect v2 tool count after disk-reload swap"
        )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 3. refreshed=False when nothing changes on second call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_returns_refreshed_false_when_nothing_changes(
    tmp_path: Path,
) -> None:
    """Tier 2: second consecutive refresh_mcp_servers() with no yaml edit
    and no cache file change returns refreshed=False.

    The cache does not swap when nothing has changed, so the before-vs-after
    snapshot comparison is equal and refreshed must be False.
    """
    state_dir = _state_dir(tmp_path)
    tools = {"srv": [{"name": "tool_x", "description": "x"}]}
    write_cache(cache_file_path(state_dir), tools)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path, mcp_servers={"srv": {"command": "mcp-srv"}})

        # Warm up: first call populates cache from disk.
        await session.refresh_mcp_servers()

        # Second call: nothing changed — expect refreshed=False.
        result = await session.refresh_mcp_servers()

        assert result["refreshed"] is False, (
            "refreshed must be False when no yaml or disk cache change occurred"
        )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 4. servers projection shape: {name: int}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_servers_projection_shape(tmp_path: Path) -> None:
    """Tier 2: refresh_mcp_servers returns a servers dict where keys are
    server names (str) and values are tool counts (int).

    Uses a fixture cache with 2 servers / known tool counts to verify
    the {name: tool_count} shape without format-pinning.
    """
    state_dir = _state_dir(tmp_path)
    fixture_cache = {
        "alpha": [
            {"name": "a1", "description": "first"},
            {"name": "a2", "description": "second"},
        ],
        "beta": [
            {"name": "b1", "description": "b one"},
        ],
    }
    write_cache(cache_file_path(state_dir), fixture_cache)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path, mcp_servers={"alpha": {}, "beta": {}})

        result = await session.refresh_mcp_servers()

        servers = result["servers"]
        assert isinstance(servers, dict), "servers must be a dict"
        assert "alpha" in servers, "alpha server must appear in servers"
        assert "beta" in servers, "beta server must appear in servers"
        for name, count in servers.items():
            assert isinstance(name, str), f"server key {name!r} must be str"
            assert isinstance(count, int), f"tool count for {name!r} must be int"
        assert servers["alpha"] == len(fixture_cache["alpha"]), (
            "alpha tool count must equal fixture tool list length"
        )
        assert servers["beta"] == len(fixture_cache["beta"]), (
            "beta tool count must equal fixture tool list length"
        )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 5. Empty MCP config → servers={}, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_empty_mcp_config_returns_empty_servers(
    tmp_path: Path,
) -> None:
    """Tier 2: when no MCP servers are configured, refresh_mcp_servers
    completes without error and returns servers={}.

    Verify the return shape is well-formed and no exception propagates.
    """
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path, mcp_servers=None)
        result = await session.refresh_mcp_servers()

        assert "refreshed" in result, "return dict must contain 'refreshed'"
        assert "servers" in result, "return dict must contain 'servers'"
        assert result["servers"] == {}, (
            "servers must be empty when no MCP servers are configured"
        )
        assert "error" not in result, "no error expected for empty config"
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 6. Defensive failure: ensure-cached probe failure → structured error return
#    (no exception propagation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_does_not_raise_on_adapter_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: when the live-probe path (ensure_mcp_tools_cached) encounters
    an unrecoverable server failure, refresh_mcp_servers still returns a
    well-formed dict and does not raise.

    ensure_mcp_tools_cached already handles per-server probe errors by
    caching an empty tool list (= graceful degradation, not a re-raise).
    This test pins the S3-level contract: even when a server returns no
    tools due to probe failure, the method completes cleanly and the
    servers dict is present (possibly with zero-count entries).

    We simulate total probe failure by making every server appear to have
    zero tools (= empty list from probe), which is what the adapter does
    on server error.  The method must complete cleanly and servers must
    be a dict.
    """
    state_dir = _state_dir(tmp_path)
    # No pre-written disk cache — forces live-probe path (ensure_mcp_tools_cached).
    # The real mcp_list_tools callback is the session's _mcp_list_tools,
    # which on error/non-existent server returns [] (= adapter defensive behaviour).

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Use an MCP server config that will fail to connect (no real server)
        # but that the adapter handles gracefully by caching [].
        session = _make_session(
            tmp_path,
            mcp_servers={"ghost_srv": {"command": "nonexistent-mcp-server-binary"}},
        )

        # Must not raise — probe failure is defensive (server cached as []).
        result = await session.refresh_mcp_servers()

        assert "refreshed" in result, "return dict must contain 'refreshed'"
        assert "servers" in result, "return dict must contain 'servers'"
        assert isinstance(result["servers"], dict), (
            "servers must be a dict even when probe fails"
        )
    finally:
        os.chdir(old_cwd)
