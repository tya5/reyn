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

import asyncio
import time
from pathlib import Path

import pytest

from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.chat.services.mcp_cache_file import cache_file_path, write_cache
from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver

# ---------------------------------------------------------------------------
# Null callbacks (same shape as in test_mcp_lazy_tools_cache.py)
# ---------------------------------------------------------------------------


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_list(path: str) -> dict:
    return {"path": path, "entries": []}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_mcp_list_servers() -> list:
    return []


async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def _null_run_skill(spec, *, chain_id) -> dict:
    return {"status": "finished", "data": {}}


async def _null_spawn_skill(spec, *, chain_id) -> dict:
    return {"status": "spawned", "run_id": "x", "chain_id": chain_id, "skill": "", "note": ""}


async def _null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


async def _null_spawn_plan_task(*, plan_id, runtime, chain_id, parent_chain_id=None) -> None:
    pass


# ---------------------------------------------------------------------------
# Callable probe class (tracks invocations without mocks)
# ---------------------------------------------------------------------------


class _CountingProbe:
    """Real async callable that records which servers were probed."""

    def __init__(self, tools_by_server: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[str] = []
        self._tools = tools_by_server or {}

    async def __call__(self, server_name: str) -> list[dict]:
        self.calls.append(server_name)
        return list(self._tools.get(server_name, []))


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    tmp_path: Path,
    mcp_servers: dict | None,
    probe: _CountingProbe,
    state_dir: Path,
) -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    return RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=mcp_servers,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=probe,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        state_dir=state_dir,
    )


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
    pre_written = {"myserver": [{"name": "disk_tool", "description": "from disk"}]}
    write_cache(cache_path, pre_written)

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

    from reyn.chat.services.mcp_cache_file import read_cache
    on_disk = read_cache(cache_path)
    assert on_disk is not None
    assert on_disk.get("srv") == live_tools


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
    write_cache(cache_path, initial_tools)

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
    write_cache(cache_path, v1_tools)

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
    write_cache(cache_path, v2_tools)

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
