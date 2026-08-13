"""Tier 2: RouterHostAdapter warm-start + turn-boundary reload — FP-0037 S1.

Pins:
  - ensure_mcp_tools_cached warm-starts from disk (no live probe) when
    the cache file is present and valid.
  - ensure_mcp_tools_cached falls back to live probe + writes file when
    cache file is absent.
  - maybe_reload_mcp_tools_cache_from_disk: absent file → no-op.
  - maybe_reload_mcp_tools_cache_from_disk: mtime unchanged → no-op.
  - maybe_reload_mcp_tools_cache_from_disk: mtime advanced → reload.

No mocks.  Probe is a real async callable; call count tracked via a
plain nonlocal counter on a tiny callable class.  Private-state access
goes through the mcp_tools_cache_snapshot property (public test surface
added in the same PR per Tier policy).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from reyn.runtime.services.mcp_cache_file import (
    ToolsAnswered,
    cache_file_path,
    write_cache,
)

# #3879 M4-0: _CountingProbe / _make_adapter (+ the null-callback duplicates
# they used) moved to tests/_support/mcp_cache_test_helpers.py (byte-identical
# apart from dropping the leading underscore and reusing router_host_adapter.py's
# existing null_* callbacks instead of a second copy of the same six) — a
# module OTHER test files import from cannot migrate as a pure git mv under
# Stage 1. Aliased back to the original module-local names so everything
# below is unchanged.
from tests._support.mcp_cache_test_helpers import (  # noqa: E402
    CountingProbe as _CountingProbe,
)
from tests._support.mcp_cache_test_helpers import (
    make_mcp_cache_adapter as _make_adapter,
)


async def _null_spawn_plan_task(*, plan_id, runtime, chain_id, parent_chain_id=None) -> None:
    pass


# ---------------------------------------------------------------------------
# 1. Warm-start from disk — no live probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_mcp_tools_cached_warm_starts_from_disk(tmp_path: Path) -> None:
    """Tier 2: when a valid cache file exists before ensure_mcp_tools_cached
    is called, the live probe callback is NOT invoked and the cache is
    populated from the file."""
    state_dir = tmp_path / "state"
    cache_path = cache_file_path(state_dir)
    disk_tools = [{"name": "disk_tool", "description": "from disk"}]
    write_cache(cache_path, {"myserver": ToolsAnswered(tools=disk_tools)})

    probe = _CountingProbe({"myserver": [{"name": "live_tool", "description": "live"}]})
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"myserver": {}},
        probe=probe,
        state_dir=state_dir,
    )

    await adapter.ensure_mcp_tools_cached()

    assert probe.calls == [], (
        "live probe must NOT be invoked when cache file is present"
    )
    snapshot = adapter.mcp_tools_cache_snapshot
    assert snapshot is not None
    assert "myserver" in snapshot
    assert snapshot["myserver"] == [{"name": "disk_tool", "description": "from disk"}]


# ---------------------------------------------------------------------------
# 2. Live probe + write to disk when cache file absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_mcp_tools_cached_writes_to_disk_after_live_probe(
    tmp_path: Path,
) -> None:
    """Tier 2: when no cache file exists, the live probe runs and its
    result is written to the cache file."""
    state_dir = tmp_path / "state"
    cache_path = cache_file_path(state_dir)
    assert not cache_path.exists()

    live_tools = [{"name": "live_tool", "description": "from probe"}]
    probe = _CountingProbe({"srv": live_tools})
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {}},
        probe=probe,
        state_dir=state_dir,
    )

    await adapter.ensure_mcp_tools_cached()

    assert "srv" in probe.calls, "live probe must run when cache file is absent"
    assert cache_path.exists(), "cache file must be written after live probe"

    from reyn.runtime.services.mcp_cache_file import read_cache
    on_disk = read_cache(cache_path)
    assert on_disk is not None
    assert on_disk["srv"].tools == live_tools


# ---------------------------------------------------------------------------
# 3. maybe_reload: absent file → no-op
# ---------------------------------------------------------------------------


def test_maybe_reload_mcp_tools_cache_from_disk_no_file_noops(
    tmp_path: Path,
) -> None:
    """Tier 2: maybe_reload_mcp_tools_cache_from_disk does nothing when
    the cache file does not exist."""
    state_dir = tmp_path / "state"
    probe = _CountingProbe()
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {}},
        probe=probe,
        state_dir=state_dir,
    )
    # Pre-populate in-memory cache via snapshot injection route:
    # we verify the snapshot is unchanged after the call.
    # Use the public get_mcp_servers() to confirm no surprise reload.
    before = adapter.get_mcp_servers()
    adapter.maybe_reload_mcp_tools_cache_from_disk()
    after = adapter.get_mcp_servers()
    assert before == after, "no-op when cache file absent"


# ---------------------------------------------------------------------------
# 4. maybe_reload: mtime unchanged → no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_reload_mcp_tools_cache_from_disk_unchanged_mtime_noops(
    tmp_path: Path,
) -> None:
    """Tier 2: when the cache file mtime has not changed since last load,
    maybe_reload_mcp_tools_cache_from_disk does not replace the cache."""
    state_dir = tmp_path / "state"
    cache_path = cache_file_path(state_dir)
    initial_tools = {"srv": [{"name": "original", "description": "v1"}]}
    write_cache(cache_path, {"srv": ToolsAnswered(tools=initial_tools["srv"])})

    probe = _CountingProbe()
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {}},
        probe=probe,
        state_dir=state_dir,
    )

    # Warm-start loads the file + records mtime.
    await adapter.ensure_mcp_tools_cached()
    assert adapter.mcp_tools_cache_snapshot == initial_tools

    # Write "fresher" data in-memory to detect a spurious reload.
    # We cannot write the same path (that would update mtime), so we
    # simply call maybe_reload without touching the file.
    adapter.maybe_reload_mcp_tools_cache_from_disk()
    # Cache must still be the same (mtime unchanged since warm-start).
    assert adapter.mcp_tools_cache_snapshot == initial_tools, (
        "cache must not be replaced when file mtime is unchanged"
    )


# ---------------------------------------------------------------------------
# 5. maybe_reload: newer mtime → reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_reload_mcp_tools_cache_from_disk_newer_mtime_reloads(
    tmp_path: Path,
) -> None:
    """Tier 2: when the cache file mtime has advanced (e.g. 'reyn mcp refresh'
    was run), maybe_reload replaces the in-memory cache with the newer data."""
    state_dir = tmp_path / "state"
    cache_path = cache_file_path(state_dir)

    v1_tools = {"srv": [{"name": "v1_tool", "description": "first version"}]}
    write_cache(cache_path, {"srv": ToolsAnswered(tools=v1_tools["srv"])})

    probe = _CountingProbe()
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {}},
        probe=probe,
        state_dir=state_dir,
    )

    # Warm-start loads v1 and records mtime.
    await adapter.ensure_mcp_tools_cached()
    assert adapter.mcp_tools_cache_snapshot == v1_tools

    # Simulate 'reyn mcp refresh' by writing a newer version of the file.
    # Sleep a tiny bit to ensure the mtime advances (filesystem resolution).
    time.sleep(0.02)
    v2_tools = {"srv": [{"name": "v2_tool", "description": "refreshed version"}]}
    write_cache(cache_path, {"srv": ToolsAnswered(tools=v2_tools["srv"])})

    # Turn-boundary call should detect the newer mtime and reload.
    adapter.maybe_reload_mcp_tools_cache_from_disk()

    snapshot = adapter.mcp_tools_cache_snapshot
    assert snapshot == v2_tools, (
        "in-memory cache must be replaced with v2 after mtime advance"
    )
    # Verify via the public surface too.
    listing = {s["name"]: s for s in adapter.get_mcp_servers()}
    assert "srv" in listing
    assert listing["srv"]["tools"] == v2_tools["srv"]
