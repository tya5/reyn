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
    # FP-0037 S1: pass an empty per-test state_dir so the warm-start path
    # never reads a stale on-disk cache from the project root (.reyn/state/).
    # Each test gets a fresh isolated directory → live probe always runs.
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
        state_dir=tmp_path / "state",
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
    assert listing["foo"]["tools"][0]["name"] == "foo_tool1"
    assert listing["foo"]["tools"][1]["name"] == "foo_tool2"
    assert listing["bar"]["tools"][0]["name"] == "bar_tool1"
    assert listing["bar"]["tools"][1]["name"] == "bar_tool2"


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


# ── 8. Task-identity safety on timeout (= user-observed anyio error fix) ───
#
# When a probe coroutine opens an AsyncExitStack (= what
# ``MCPClient.initialize()`` does under the hood; the stack wraps the
# stdio_client / streamablehttp_client transport and the mcp SDK's
# ClientSession), the cleanup runs on cancellation. Pre-fix this used
# ``asyncio.wait_for`` which wraps the awaited coroutine in a new asyncio
# task in some scenarios. The anyio cancel scopes inside the mcp SDK
# checked current_task() at __aexit__ and raised
# ``RuntimeError: Attempted to exit cancel scope in a different task
# than it was entered in``. ``asyncio.timeout()`` is a task-local
# deadline — cancellation is raised at the awaiter in the SAME task —
# so the AsyncExitStack unwinds cleanly.


@pytest.mark.asyncio
async def test_timeout_does_not_leak_cancel_scope_error_on_async_exit_stack(tmp_path):
    """Tier 2: a probe holding an AsyncExitStack across a timeout
    survives cancellation without raising a ``RuntimeError: Attempted
    to exit cancel scope in a different task...``.

    Simulates the production path where ``MCPClient.initialize()``
    opens an ``AsyncExitStack`` (and the mcp SDK opens anyio cancel
    scopes inside it). With ``asyncio.wait_for`` (pre-fix), the
    cleanup ran in a different task and the RuntimeError leaked. With
    ``asyncio.timeout()`` (post-fix), cleanup runs in the same task
    and the stack closes cleanly — the timeout path just caches ``[]``.
    """
    from contextlib import AsyncExitStack

    entered_tasks: list[object] = []
    exited_tasks: list[object] = []

    async def _slow_probe(server: str) -> list[dict]:
        stack = AsyncExitStack()
        await stack.__aenter__()
        entered_tasks.append(asyncio.current_task())
        try:
            # Sleep past the per-server timeout to force cancellation
            # mid-AsyncExitStack hold.
            await asyncio.sleep(2.0)
        finally:
            exited_tasks.append(asyncio.current_task())
            await stack.aclose()
        return []

    adapter = _make_adapter_with_mcp(
        tmp_path=tmp_path,
        mcp_servers={"slow": {}},
        mcp_list_tools_cb=_slow_probe,
    )
    # If asyncio.wait_for were still in use, this call would either
    # raise or log a RuntimeError when the stack cleanup ran in a
    # different task than the entry. asyncio.timeout() keeps the entry
    # and exit on the same task.
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert entered_tasks == exited_tasks, (
        "AsyncExitStack must enter and exit in the SAME asyncio.Task; "
        f"entered={entered_tasks!r} exited={exited_tasks!r}"
    )
    listing = {s["name"]: s for s in adapter.get_mcp_servers()}
    assert listing["slow"]["tools"] == [], (
        "slow server must be cached as [] after timeout"
    )
