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
from reyn.mcp.client import MCPClient
from reyn.mcp.connection_service import MCPConnectionService
from reyn.mcp.message_handler import ReynMCPMessageHandler
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
)
from tests._support.events import collect_events

# #3482: RouterHostAdapter's op-context/mcp-gateway constructor params were
# bundled into two frozen, default-free dataclasses. These module-level
# constants are the "all fields unset" instances this file's tests reuse.
from tests._support.paths import REPO_ROOT
from tests._support.router_host_adapter import make_op_context_source  # noqa: E402

_EMPTY_OP_CTX = make_op_context_source()
_EMPTY_MCP_GATEWAY = McpGatewayInputs(
    mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
)


_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


def _make_adapter(*, tmp_path: Path, events: EventLog) -> RouterHostAdapter:
    """Real RouterHostAdapter with one configured server ("srv"), an isolated
    per-test state_dir (never reads a stale on-disk tools cache), and a probe
    callback that returns a fixed tool list — mirrors
    tests/core/test_mcp_lazy_tools_cache.py's construction helper."""

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
    adapter = RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers={"srv": {}},
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        state_dir=tmp_path / "state",
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )
    # #3447: mcp_list_tools is now a real RouterHostAdapter method — see the
    # same-shaped note in test_mcp_lazy_tools_cache.py's _make_adapter_with_mcp.
    adapter.mcp_list_tools = _probe
    return adapter


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
    collected = collect_events(events)
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

        # #3748: unbounded (owner policy) -- wait for on_tool_list_changed to
        # invalidate the lazy MCP tools cache. No terminating assert: the loop
        # condition IS that check, so an assert restating it can never fire; a
        # hang here surfaces via the kill stack showing this exact `while`.
        while adapter.mcp_tools_cache_snapshot is not None:
            await asyncio.sleep(0.02)

        matching = [e for e in collected if e.type == "mcp_tool_list_changed"]
        (only_event,) = matching  # exactly one — the single real notification sent
        assert only_event.data.get("server") == "srv"
    finally:
        await service.aclose()


# ── (b) real server-pushed progress: per-call callback is the ONE mcp_progress
#        source; the bridge does not double-emit (#2597 F2) ───────────────────────


@pytest.mark.asyncio
async def test_progress_notification_not_double_emitted_by_bridge(tmp_path: Path):
    """Tier 2: #2597 F2 — a REAL server-pushed ``notifications/progress`` on a held
    connection with BOTH a per-call ``progress_callback`` (the ``op_runtime/mcp.py``
    path, which itself emits ``mcp_progress`` with tool-name context) AND the
    S2b notifications bridge installed simultaneously must NOT double-emit
    ``mcp_progress`` — a live probe proved the SDK dual-delivers each in-call
    progress notification to BOTH the per-call callback AND the installed
    ``message_handler`` (``ReynMCPMessageHandler.on_progress``); emitting from both
    would double every in-call progress event on the EventLog. The bridge is fixed to
    never emit ``mcp_progress`` (see ``ReynMCPMessageHandler.on_progress``'s
    docstring) — this test proves that with the caller's own per-call callback
    ALSO emitting (mirroring ``op_runtime/mcp.py``'s ``_on_progress``), exactly ONE
    ``mcp_progress`` event lands per reported progress step, not two."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _CFG)

        async def _progress_cb(progress, total, message) -> None:
            # Mirrors op_runtime/mcp.py's _on_progress — the ONE place that should
            # emit mcp_progress for call-scoped progress.
            events.emit(
                "mcp_progress", server="srv", tool="progress",
                progress=progress, total=total, message=message,
            )

        result = await client.call_tool(
            "progress", {"steps": 2}, progress_callback=_progress_cb,
        )
        assert result["isError"] is False

        matching = [e for e in collected if e.type == "mcp_progress"]
        first_step, second_step = matching  # exactly ONE event per progress step, not two
        assert first_step.data.get("server") == "srv"
        assert second_step.data.get("server") == "srv"
        assert first_step.data.get("tool") == "progress", (
            "the surviving mcp_progress event carries tool-name context — "
            "proof it came from the per-call callback, not the (tool-blind) bridge"
        )
        assert (first_step.data.get("progress"), second_step.data.get("progress")) == (
            1.0, 2.0,
        )
        assert (
            first_step.data.get("message"), second_step.data.get("message"),
        ) == ("step-1", "step-2")
    finally:
        await service.aclose()


# ── (c) task-status routing (#3698 P3: composed, not inherited) ────────────────────


class _FakeClient:
    """Duck-typed stand-in for fastmcp.Client — only the ONE method
    ReynMCPMessageHandler.__call__ actually calls for a task-status notification
    (``_handle_task_status_notification``). A real (hand-written) object, not a
    Mock/patch."""

    def __init__(self) -> None:
        self.routed: list[types.TaskStatusNotification] = []

    def _handle_task_status_notification(self, root) -> None:
        self.routed.append(root)


def _make_task_status_notification() -> types.TaskStatusNotification:
    from datetime import datetime, timezone

    # #4412 pin-bump PR: created_at/last_updated_at are `str` (ISO format) on
    # 2.0, not `datetime` — confirmed live via model_fields (a real shape
    # change alongside the camelCase->snake_case rename, not just the rename).
    now = datetime.now(timezone.utc).isoformat()
    params = types.TaskStatusNotificationParams(
        task_id="task-1",
        status="working",
        created_at=now,
        last_updated_at=now,
        ttl=None,
    )
    return types.TaskStatusNotification(params=params)


@pytest.mark.asyncio
@pytest.mark.skip(
    reason="#4457: mcp 2.0's IncomingMessage/ServerNotification unions "
    "exclude TaskStatusNotification -- the real routing mechanism "
    "(Dispatcher/notification_bindings) is marked provisional by the SDK "
    "itself; deferred until that surface stabilizes, tracked in #4457.",
)
async def test_task_status_routing_via_composed_call():
    """Tier 1: #3698 P3 — ReynMCPMessageHandler no longer inherits fastmcp's
    TaskNotificationHandler; it implements ``__call__`` itself and peeks for a
    TaskStatusNotification, forwarding it to the bound client's
    ``_handle_task_status_notification`` directly (the same SEP-1686 routing
    the inherited version used to get for free). Drives the REAL ``__call__``
    entry point — the same one ``mcp.client.session.ClientSession`` invokes
    as ``self._message_handler(req)`` — and observes the routing side effect
    on a real fake client object, not a private-state assertion."""
    handler = ReynMCPMessageHandler(lambda *a, **k: None, "srv")

    fake_client = _FakeClient()
    handler.bind_client(fake_client)

    notification = _make_task_status_notification()
    await handler(notification)

    (routed,) = fake_client.routed  # exactly one status notification was dispatched
    assert routed.params.taskId == "task-1"


@pytest.mark.asyncio
async def test_initialize_stdio_actually_binds_the_message_handler_to_the_real_client():
    """Tier 2: #4836 — ``MCPClient._initialize_stdio``'s production call site
    actually invokes ``bind_client(client)`` with the REAL ``mcp.Client`` it
    just constructed, not merely that :meth:`ReynMCPMessageHandler.bind_client`
    works in isolation when called directly (the sibling tests in this file,
    and #4457's own skipped test, only ever call it by hand against a fake).
    #4282 retired fastmcp from the client path and, with it, the ONE call site
    that used to invoke ``bind_client`` at all — restored here targeting the
    official SDK's ``Client`` instead. Real stdio connection (the same echo
    server this file's other tests use), real ``MCPClient.initialize()`` —
    the same production entrypoint ``MCPConnectionService``/every live chat
    turn goes through — asserted via the handler's own public
    :meth:`~reyn.mcp.message_handler.ReynMCPMessageHandler.is_bound`, not a
    reach into either object's private state.

    Falsify (owner-instructed weak-witness shape, mcp 2.0 doesn't currently
    deliver ``TaskStatusNotification`` at all — #4457 — so the binding's
    actual CONSUMER can't be witnessed end-to-end today): stripping the
    restored ``bind_client`` call from ``_initialize_stdio`` flips this test
    red (``is_bound()`` stays False, the weakref target is never set) while
    every other test in this file's own bucket stays green — confirmed by
    hand before landing."""
    handler = ReynMCPMessageHandler(lambda *a, **k: None, "srv")
    assert not handler.is_bound(), "unbound until a real client actually binds"

    async with MCPClient(_CFG, message_handler=handler) as client:
        assert client.is_initialized()
        assert handler.is_bound(), (
            "the real production init path must call bind_client() with the "
            "client it just constructed, not leave the two-phase binding "
            "incomplete"
        )


@pytest.mark.asyncio
async def test_unrecognized_notification_is_logged_not_silently_dropped(caplog) -> None:
    """Tier 1: #3698 P3 review condition (lead-coder) — a message shape the
    handler's closed match-list doesn't act on must be OBSERVABLE (a debug log
    line naming the type), not silently reach nothing. Distinguishes "processed
    and produced no side effect" (the 4 acted-on shapes, when their own body
    happens to do nothing) from "not one of ours, ignored" — the day fastmcp/MCP
    adds a notification type this handler doesn't yet act on, that day must be
    distinguishable in the log from an ordinary quiet run.

    ``ResourceListChangedNotification`` — a real MCP notification shape reyn
    has never acted on (module docstring: "out of ②b's scope") — is the probe."""
    import logging

    handler = ReynMCPMessageHandler(lambda *a, **k: None, "srv")
    handler.bind_client(_FakeClient())

    notification = types.ResourceListChangedNotification()
    with caplog.at_level(logging.DEBUG, logger="reyn.mcp.message_handler"):
        await handler(notification)

    assert any(
        "no handler for message type" in r.message and "ResourceListChangedNotification" in r.message
        for r in caplog.records
    ), f"expected an observable unhandled-message log line; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_a_non_notification_message_is_also_logged_not_silently_dropped(caplog) -> None:
    """Tier 1: same condition as above, for the OTHER branch of __call__ — a
    non-ServerNotification message (a request or a transport-level Exception,
    per ``mcp.types``' own ``Message`` union) also reaches _log_unhandled
    rather than nothing, since reyn has never acted on either shape (no
    on_request/on_ping/on_exception override existed even in the inherited
    version)."""
    import logging

    handler = ReynMCPMessageHandler(lambda *a, **k: None, "srv")
    handler.bind_client(_FakeClient())

    with caplog.at_level(logging.DEBUG, logger="reyn.mcp.message_handler"):
        await handler(RuntimeError("not a ServerNotification"))

    assert any("no handler for message type" in r.message for r in caplog.records)


# ── (d) synchronous handler body — sink faults never escape __call__ ──────────────


@pytest.mark.asyncio
async def test_emit_sink_fault_does_not_break_call():
    """Tier 1: the notification hooks call the emit sink SYNCHRONOUSLY (never
    ``await`` it — see message_handler.py's module docstring) and never let a sink
    fault escape: a raising sink must not propagate out of ``__call__()``, since
    that would crash/stall the held connection's FastMCP receive loop for every
    subsequent message, not just the one notification that triggered the fault."""

    def _boom(*args, **kwargs):
        raise RuntimeError("sink exploded")

    handler = ReynMCPMessageHandler(_boom, "srv")
    handler.bind_client(_FakeClient())

    notification = types.ToolListChangedNotification()
    await handler(notification)  # must not raise


@pytest.mark.asyncio
async def test_tools_cache_invalidate_fault_does_not_block_event_emit(tmp_path: Path):
    """Tier 1: a faulting ``tools_cache_invalidate`` callback must not prevent the
    ``mcp_tool_list_changed`` event from still being emitted — the two side effects
    are independent and one failing must not silently swallow the other."""
    events = EventLog(subscribers=[])
    collected = collect_events(events)

    def _boom_invalidate(server: str) -> None:
        raise RuntimeError("cache invalidation exploded")

    handler = ReynMCPMessageHandler(
        lambda et, **d: events.emit(et, **d), "srv",
        tools_cache_invalidate=_boom_invalidate,
    )
    handler.bind_client(_FakeClient())

    notification = types.ToolListChangedNotification()
    await handler(notification)  # must not raise

    matching = [e for e in collected if e.type == "mcp_tool_list_changed"]
    (only_event,) = matching  # exactly one — invalidate faulting must not swallow the emit
    assert only_event.data.get("server") == "srv"
