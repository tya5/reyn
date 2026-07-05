"""Tests for the async server->client notifications bridge (#2597 S2b).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``. The
notification-carrying tests spawn a REAL subprocess running
``tests/_support/mcp_fastmcp_echo_server.py`` (a real FastMCP server) whose
``notify_tool_list_changed`` / ``notify_prompt_list_changed`` / ``progress`` tools send
REAL SEP-1686 notifications over the wire on a held (S2a) connection — proving
``ReynMCPMessageHandler`` actually receives server-pushed notifications on a held
connection, not just that its methods work if called directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import mcp.types as types
import pytest

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.mcp.connection_service import MCPConnectionService
from reyn.mcp.message_handler import ReynMCPMessageHandler
from reyn.runtime.services import MemoryService, RouterHostAdapter

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}


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


async def _null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


def _make_adapter(*, tmp_path: Path, events: EventLog) -> RouterHostAdapter:
    """Real RouterHostAdapter with one configured server ("srv"), an isolated
    per-test state_dir (never reads a stale on-disk tools cache), and a probe
    callback that returns a fixed tool list — mirrors
    tests/test_mcp_lazy_tools_cache.py's construction helper."""

    async def _probe(server: str) -> list[dict]:
        return [{"name": f"{server}_tool", "description": "d"}]

    async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
        return {}

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
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers={"srv": {}},
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=_probe,
        mcp_call_tool=_null_mcp_call_tool,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        state_dir=tmp_path / "state",
    )


# ── (a) real server-pushed tools/list_changed -> event + cache invalidation ────────


@pytest.mark.asyncio
async def test_tool_list_changed_notification_emits_event_and_invalidates_cache(
    tmp_path: Path,
):
    """Tier 2: a REAL server-pushed ``notifications/tools/list_changed`` on a held
    (S2a) connection lands as an ``mcp_tool_list_changed`` event on the session's
    EventLog AND invalidates the RouterHostAdapter's lazy MCP tools cache (#160/
    FP-0037), so the next ``ensure_mcp_tools_cached()`` re-probes instead of serving
    the now-possibly-stale cached list."""
    events = EventLog(subscribers=[])
    adapter = _make_adapter(tmp_path=tmp_path, events=events)

    service = MCPConnectionService(
        emit_sink=lambda et, **d: events.emit(et, **d),
        tools_cache_invalidate=adapter.invalidate_mcp_tools_cache,
    )
    try:
        # Populate the cache first so invalidation has something to undo.
        await adapter.ensure_mcp_tools_cached()
        assert adapter.mcp_tools_cache_snapshot == {
            "srv": [{"name": "srv_tool", "description": "d"}]
        }

        client = await service.get("srv", _CFG)
        result = await client.call_tool("notify_tool_list_changed", {})
        assert result["isError"] is False

        # The notification is delivered asynchronously on FastMCP's session_task;
        # give the event loop a beat to run the receive loop's callback.
        import asyncio

        for _ in range(50):
            if adapter.mcp_tools_cache_snapshot is None:
                break
            await asyncio.sleep(0.02)

        matching = [e for e in events.all() if e.type == "mcp_tool_list_changed"]
        (only_event,) = matching  # exactly one — the single real notification sent
        assert only_event.data.get("server") == "srv"
        assert adapter.mcp_tools_cache_snapshot is None, (
            "on_tool_list_changed must invalidate the lazy MCP tools cache"
        )
    finally:
        await service.aclose()


# ── (b) real server-pushed progress -> mcp_progress event ─────────────────────────


@pytest.mark.asyncio
async def test_progress_notification_emits_mcp_progress_event(tmp_path: Path):
    """Tier 2: a REAL server-pushed ``notifications/progress`` on a held connection
    lands as an ``mcp_progress`` event on the EventLog — the SAME event type
    ``op_runtime/mcp.py`` already emits for the per-call progress_handler path
    (issue #264/#271), so ``ChatLifecycleForwarder.on_mcp_progress`` surfaces it
    without a new consumer."""
    events = EventLog(subscribers=[])
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _CFG)

        async def _progress_cb(progress, total, message) -> None:
            pass

        result = await client.call_tool(
            "progress", {"steps": 2}, progress_callback=_progress_cb,
        )
        assert result["isError"] is False

        matching = [e for e in events.all() if e.type == "mcp_progress"]
        first_step, second_step = matching  # one event per reported progress step
        assert first_step.data.get("server") == "srv"
        assert second_step.data.get("server") == "srv"
        assert (first_step.data.get("progress"), second_step.data.get("progress")) == (
            1.0, 2.0,
        )
        assert (
            first_step.data.get("message"), second_step.data.get("message"),
        ) == ("step-1", "step-2")
    finally:
        await service.aclose()


# ── (c) subclassing TaskNotificationHandler preserves task-status routing ─────────


class _FakeClient:
    """Duck-typed stand-in for fastmcp.Client — only the ONE method
    TaskNotificationHandler.dispatch actually calls (``_handle_task_status_
    notification``). A real (hand-written) object, not a Mock/patch."""

    def __init__(self) -> None:
        self.routed: list[types.TaskStatusNotification] = []

    def _handle_task_status_notification(self, root) -> None:
        self.routed.append(root)


def _make_task_status_notification() -> types.ServerNotification:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    params = types.TaskStatusNotificationParams(
        taskId="task-1",
        status="working",
        createdAt=now,
        lastUpdatedAt=now,
        ttl=None,
    )
    return types.ServerNotification(types.TaskStatusNotification(params=params))


@pytest.mark.asyncio
async def test_task_status_routing_preserved_through_subclass():
    """Tier 1: ReynMCPMessageHandler subclasses TaskNotificationHandler and does NOT
    override ``dispatch`` — so TaskNotificationHandler's own SEP-1686 task-status
    routing (peek for a TaskStatusNotification, forward to the bound client, THEN
    fall through to the base MessageHandler match/case that invokes our hooks) keeps
    running unmodified. Drives the REAL inherited ``dispatch()`` (not a private-state
    assertion) and observes the routing side effect on a real fake client object."""
    from fastmcp.client.tasks import TaskNotificationHandler

    handler = ReynMCPMessageHandler(lambda *a, **k: None, "srv")
    assert isinstance(handler, TaskNotificationHandler)

    fake_client = _FakeClient()
    handler.bind_client(fake_client)

    notification = _make_task_status_notification()
    await handler.dispatch(notification)

    (routed,) = fake_client.routed  # exactly one status notification was dispatched
    assert routed.params.taskId == "task-1"


# ── (d) synchronous handler body — sink faults never escape dispatch ──────────────


@pytest.mark.asyncio
async def test_emit_sink_fault_does_not_break_dispatch():
    """Tier 1: the notification hooks call the emit sink SYNCHRONOUSLY (never
    ``await`` it — see message_handler.py's module docstring) and never let a sink
    fault escape: a raising sink must not propagate out of ``dispatch()``, since
    that would crash/stall the held connection's FastMCP receive loop for every
    subsequent message, not just the one notification that triggered the fault."""

    def _boom(*args, **kwargs):
        raise RuntimeError("sink exploded")

    handler = ReynMCPMessageHandler(_boom, "srv")
    handler.bind_client(_FakeClient())

    notification = types.ServerNotification(types.ToolListChangedNotification())
    await handler.dispatch(notification)  # must not raise


@pytest.mark.asyncio
async def test_tools_cache_invalidate_fault_does_not_block_event_emit(tmp_path: Path):
    """Tier 1: a faulting ``tools_cache_invalidate`` callback must not prevent the
    ``mcp_tool_list_changed`` event from still being emitted — the two side effects
    are independent and one failing must not silently swallow the other."""
    events = EventLog(subscribers=[])

    def _boom_invalidate(server: str) -> None:
        raise RuntimeError("cache invalidation exploded")

    handler = ReynMCPMessageHandler(
        lambda et, **d: events.emit(et, **d), "srv",
        tools_cache_invalidate=_boom_invalidate,
    )
    handler.bind_client(_FakeClient())

    notification = types.ServerNotification(types.ToolListChangedNotification())
    await handler.dispatch(notification)  # must not raise

    matching = [e for e in events.all() if e.type == "mcp_tool_list_changed"]
    (only_event,) = matching  # exactly one — invalidate faulting must not swallow the emit
    assert only_event.data.get("server") == "srv"
