"""Tier 2: RouterHostAdapter MCP tools lazy cache — issue #160 / FP-0037.

Pins the contract for `ensure_mcp_tools_cached()`:
  - First call probes every configured MCP server in parallel and stores
    the result in `_mcp_tools_cache`.
  - Subsequent calls are no-ops (= idempotent).
  - When no MCP servers are configured, the cache becomes `{}` (= still
    not None, so the no-op branch fires next time).
  - Per-server timeout caps slow probes; on timeout / exception the
    server is cached as `[]` (= no retries, no cascading failures).
  - `_get_mcp_servers_for_router` includes `tools` field only when the
    cache for that server is populated.

No mocks — uses real async callables and a real RouterHostAdapter via
the existing `_make_adapter` helper pattern.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver


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


def _make_adapter_with_mcp(
    *,
    tmp_path: Path,
    mcp_servers: dict | None,
    mcp_list_tools_cb,
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
        mcp_list_tools=mcp_list_tools_cb,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
    )


# ── 1. No-server case ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_servers_no_probe(tmp_path):
    """Tier 2: with no MCP servers configured, ensure_mcp_tools_cached
    completes without invoking the probe callback. Public listing stays
    empty.
    """
    probe_calls: list[str] = []

    async def _probe(server: str) -> list[dict]:
        probe_calls.append(server)
        return []

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path, mcp_servers=None, mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached()
    assert probe_calls == [], "must not probe when there are zero servers"
    assert adapter.get_mcp_servers() == []


# ── 2. With servers — parallel probe + populate ────────────────────────────


@pytest.mark.asyncio
async def test_parallel_probe_populates_cache(tmp_path):
    """Tier 2: all configured servers are probed in parallel; results are
    cached per server name."""
    probe_calls: list[str] = []

    async def _probe(server: str) -> list[dict]:
        probe_calls.append(server)
        return [
            {"name": f"{server}_tool1", "description": f"tool 1 of {server}"},
            {"name": f"{server}_tool2", "description": f"tool 2 of {server}"},
        ]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"foo": {}, "bar": {}},
        mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached()
    assert set(probe_calls) == {"foo", "bar"}
    listing = {s["name"]: s for s in adapter.get_mcp_servers()}
    assert "foo" in listing and "bar" in listing
    assert len(listing["foo"]["tools"]) == 2
    assert listing["foo"]["tools"][0]["name"] == "foo_tool1"
    assert len(listing["bar"]["tools"]) == 2


# ── 3. Idempotent — second call is a no-op ────────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_second_call_no_probe(tmp_path):
    """Tier 2: once cached, subsequent calls do NOT re-probe."""
    probe_calls: list[str] = []

    async def _probe(server: str) -> list[dict]:
        probe_calls.append(server)
        return [{"name": "t", "description": "d"}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"foo": {}},
        mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached()
    first_calls = list(probe_calls)
    await adapter.ensure_mcp_tools_cached()
    assert probe_calls == first_calls, (
        "second ensure_mcp_tools_cached() must not invoke probes again"
    )


# ── 4. Timeout — slow server cached as empty, doesn't block others ────────


@pytest.mark.asyncio
async def test_slow_server_times_out_and_others_proceed(tmp_path):
    """Tier 2: a server slower than per_server_timeout is cached as [];
    other servers are not affected.

    Verifies parallel + per-server timeout discipline.
    """
    async def _probe(server: str) -> list[dict]:
        if server == "slow":
            await asyncio.sleep(2.0)  # exceeds the 0.1s timeout below
            return [{"name": "shouldnt_be_seen", "description": ""}]
        return [{"name": f"{server}_tool", "description": ""}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"fast": {}, "slow": {}},
        mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.1)
    listing = {s["name"]: s for s in adapter.get_mcp_servers()}
    assert listing["fast"]["tools"] == [
        {"name": "fast_tool", "description": ""}
    ]
    assert listing["slow"]["tools"] == [], (
        "slow server must be reported as empty tools list, not retain leaked tools"
    )


# ── 5. Exception — broken server cached as empty, doesn't propagate ────────


@pytest.mark.asyncio
async def test_probe_exception_cached_as_empty(tmp_path):
    """Tier 2: probe exception is caught and the server is cached as [].
    Adapter must never raise from ensure_mcp_tools_cached().
    """
    async def _probe(server: str) -> list[dict]:
        if server == "broken":
            raise RuntimeError("connection refused")
        return [{"name": f"{server}_tool", "description": ""}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"good": {}, "broken": {}},
        mcp_list_tools_cb=_probe,
    )
    # Must not raise
    await adapter.ensure_mcp_tools_cached()
    listing = {s["name"]: s for s in adapter.get_mcp_servers()}
    assert listing["good"]["tools"] == [
        {"name": "good_tool", "description": ""}
    ]
    assert listing["broken"]["tools"] == []


# ── 6. Error-shape entries (= [{"error": "..."}]) are filtered ────────────


@pytest.mark.asyncio
async def test_error_shape_entries_are_filtered(tmp_path):
    """Tier 2: when _mcp_list_tools_cb returns [{"error": "..."}] (= the
    contract for "connection failed but didn't raise"), the cache excludes
    error sentinels.
    """
    async def _probe(server: str) -> list[dict]:
        return [{"error": "server unreachable"}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"foo": {}},
        mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached()
    listing = adapter.get_mcp_servers()
    assert listing[0]["tools"] == [], (
        "error sentinels from _mcp_list_tools must be filtered, "
        "leaving an empty list (= same shape as timeout / exception cases)"
    )


# ── 7. _get_mcp_servers_for_router includes tools when cached ──────────────


@pytest.mark.asyncio
async def test_get_mcp_servers_excludes_tools_pre_probe(tmp_path):
    """Tier 2: before `ensure_mcp_tools_cached()` is called, the public
    listing omits the `tools` field (= pre-cache shape).
    """
    async def _probe(server: str) -> list[dict]:
        return [{"name": "t", "description": "d"}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"foo": {"description": "FooServer"}},
        mcp_list_tools_cb=_probe,
    )
    result = adapter.get_mcp_servers()
    assert len(result) == 1
    assert result[0]["name"] == "foo"
    assert "tools" not in result[0], (
        "tools field must be omitted before the lazy cache is populated"
    )


@pytest.mark.asyncio
async def test_get_mcp_servers_includes_tools_post_probe(tmp_path):
    """Tier 2: after `ensure_mcp_tools_cached()`, listing includes per-
    server `tools` array consumed by `_enumerate_category("mcp.tool")`
    and the `mcp.tool__*` direct-alias builder.
    """
    async def _probe(server: str) -> list[dict]:
        return [{"name": f"{server}_tool", "description": "x"}]

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"foo": {"description": "FooServer"}},
        mcp_list_tools_cb=_probe,
    )
    await adapter.ensure_mcp_tools_cached()
    result = adapter.get_mcp_servers()
    assert result[0]["tools"] == [{"name": "foo_tool", "description": "x"}]
