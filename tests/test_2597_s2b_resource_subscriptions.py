"""Tests for #2597 slice ②b — MCP resource subscriptions.

Real instances only, per the testing policy: no ``unittest.mock`` / ``MagicMock`` /
``AsyncMock`` / ``patch``. The subscribe/push/reconnect tests spawn a REAL MCP
server subprocess (``tests/_support/mcp_subscribable_resources_server.py`` — a
low-level ``mcp.server.lowlevel.Server`` that actually advertises
``resources.subscribe=True`` and pushes a REAL ``notifications/resources/updated``
via ``ServerSession.send_resource_updated`` when its ``bump_and_notify`` tool is
called) through a REAL ``MCPConnectionService`` (the held-connection production
path — not a bare one-shot ``MCPGateway()``/``MCPClientPool``, per the ②a lesson
that a held-handle-only method silently AttributeErrors against a bare pool).

The fail-fast-without-subscribe-capability test uses the EXISTING
``mcp_fastmcp_echo_server.py`` double: verified (see
``mcp_subscribable_resources_server.py``'s module docstring) that the base mcp
SDK hard-codes ``resources.subscribe=False`` for every server built with
FastMCP's high-level ``FastMCP()`` class, so the echo server is a real
"resources capability present, subscribe sub-capability absent" double without
needing a new server for that case.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.mcp.client import MCPCapabilityError, MCPClient
from reyn.mcp.connection_service import MCPConnectionService
from reyn.mcp.gateway import MCPFault, MCPGateway
from reyn.schemas.models import MCPSubscribeResourceIROp, MCPUnsubscribeResourceIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

_SUPPORT_DIR = Path(__file__).parent / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_URI = "resource://counter"


class _UnusedBus(InterventionBus):
    """A real InterventionBus that fails the test if ever actually invoked —
    mirrors test_2597_s2a_mcp_resources_consumption.py's helper."""

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        raise AssertionError(f"intervention bus should not be consulted: {iv}")


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


def _run(coro):
    return asyncio.run(coro)


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.02) -> None:
    """Poll ``predicate()`` until True or give up — the notification arrives
    asynchronously on FastMCP/the SDK's receive loop, not synchronously with the
    triggering call (mirrors test_2597_s2b_mcp_notifications_bridge.py's pattern)."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


# ── Tier 1: MCPClient — real subscribe/unsubscribe round-trip ─────────────────


def test_subscribe_resource_succeeds_against_a_real_subscribable_server():
    """Tier 1: MCPClient.subscribe_resource against a real server that
    advertises resources.subscribe=True succeeds (no exception)."""

    async def _it():
        async with MCPClient(_stdio_cfg(_SUBSCRIBABLE_SERVER)) as client:
            await client.subscribe_resource(_URI)
            await client.unsubscribe_resource(_URI)

    _run(_it())  # must not raise


def test_subscribe_resource_fails_fast_without_subscribe_subcapability():
    """Tier 1: the #2597 ②b gate blocks subscribe_resource against a real server
    that advertises "resources" (list/read work) but NOT the subscribe
    sub-capability — a clear MCPCapabilityError, not a raw protocol error."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_ECHO_SERVER), server_name="echo-srv",
        ) as client:
            await client.subscribe_resource("resource://pid")

    with pytest.raises(MCPCapabilityError) as exc_info:
        _run(_it())
    message = str(exc_info.value)
    assert "subscribe" in message
    assert "echo-srv" in message


def test_unsubscribe_resource_fails_fast_without_subscribe_subcapability():
    """Tier 1: same gate for unsubscribe_resource."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_ECHO_SERVER), server_name="echo-srv",
        ) as client:
            await client.unsubscribe_resource("resource://pid")

    with pytest.raises(MCPCapabilityError):
        _run(_it())


# ── Tier 2: MCPGateway pass-throughs ───────────────────────────────────────────


def test_gateway_subscribe_and_unsubscribe_round_trip():
    """Tier 2: MCPGateway.subscribe_resource/unsubscribe_resource run through
    the same fault-contained seam as read_resource."""
    gateway = MCPGateway()

    async def _it():
        await gateway.subscribe_resource("srv", _URI, _stdio_cfg(_SUBSCRIBABLE_SERVER))

    _run(_it())  # must not raise


def test_gateway_subscribe_raises_mcp_fault_on_ungated_server():
    """Tier 2: the gateway surfaces the sub-capability-gate failure as
    MCPFault (its ONE contained-fault type), never a bare MCPCapabilityError."""
    gateway = MCPGateway()

    async def _it():
        await gateway.subscribe_resource("echo-srv", "resource://pid", _stdio_cfg(_ECHO_SERVER))

    with pytest.raises(MCPFault):
        _run(_it())


# ── Tier 2: held connection — subscribe -> real server push -> EventLog event ──


@pytest.mark.asyncio
async def test_held_connection_subscribe_receives_real_push_as_event():
    """Tier 2: the CORE ②b coverage. A REAL subscribe through a REAL
    MCPConnectionService (the production held-connection path), then a REAL
    server-triggered notifications/resources/updated push lands as an
    mcp_resource_updated event on the session's EventLog — proving the whole
    chain (MCPClient.subscribe_resource -> ReynMCPMessageHandler.
    on_resource_updated -> emit_sink -> EventLog) end to end."""
    events = EventLog(subscribers=[])
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)
        assert service.subscribed_uris("srv") == [_URI]

        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in events.all())
        )

        matching = [e for e in events.all() if e.type == "mcp_resource_updated"]
        (only_event,) = matching  # exactly one push for one bump_and_notify call
        assert only_event.data.get("server") == "srv"
        assert only_event.data.get("uri") == _URI
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_held_connection_unsubscribe_untracks_uri():
    """Tier 2: unsubscribe_resource removes the URI from the service's tracked
    set (public introspection surface — subscribed_uris — never private state)."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)
        assert service.subscribed_uris("srv") == [_URI]

        await client.unsubscribe_resource(_URI)
        assert service.subscribed_uris("srv") == []
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_subscription_survives_transport_death_reconnect():
    """Tier 2: THE core ②b resilience proof. Subscribe, kill the subprocess
    (genuine transport death -> F1 heal), then a server-side push on the FRESH
    (reconnected) connection still produces an mcp_resource_updated event —
    proving MCPConnectionService re-issued the subscribe against the new
    mcp.ClientSession (which otherwise has no memory of the old session's
    subscriptions)."""
    events = EventLog(subscribers=[])
    service = MCPConnectionService(emit_sink=lambda et, **d: events.emit(et, **d))
    try:
        client = await service.get("srv", _stdio_cfg(_SUBSCRIBABLE_SERVER))
        await client.subscribe_resource(_URI)

        # Genuine transport death: the server's "die" tool os._exit()s mid-call, so
        # this call itself fails with MCPError (its response never arrives) — that
        # is the expected shape, not a test bug (mirrors mcp_fastmcp_echo_server.py's
        # "die" tool usage in test_2597_f1_heal_transport_classifier.py).
        from reyn.mcp.client import MCPError

        with pytest.raises(MCPError):
            await client.call_tool("die", {})

        # The NEXT call against the held handle triggers _heal -> reconnect, which
        # (per _ensure_open's re-subscribe loop) re-issues subscribe_resource for
        # every URI this test tracked BEFORE re-running the call itself.
        result = await client.call_tool("bump_and_notify", {})
        assert result["isError"] is False

        await _wait_for(
            lambda: any(e.type == "mcp_resource_updated" for e in events.all())
        )
        matching = [e for e in events.all() if e.type == "mcp_resource_updated"]
        assert matching, (
            "a server-side push on the RECONNECTED session produced no "
            "mcp_resource_updated event — the subscription did not survive the "
            "transport-death reconnect"
        )
        assert service.subscribed_uris("srv") == [_URI], (
            "the tracked subscription set itself must also survive the reconnect"
        )
    finally:
        await service.aclose()


# ── Tier 2: op_runtime mcp_subscribe_resource / mcp_unsubscribe_resource ──────


def _make_ctx(
    events: EventLog,
    *,
    resolver: "PermissionResolver | None",
    decl: "PermissionDecl | None" = None,
    connection_service=None,
) -> OpContext:
    ctx = OpContext(
        workspace=Workspace(events=events),
        events=events,
        permission_decl=decl or PermissionDecl(),
        permission_resolver=resolver,
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        actor="chat_router",
    )
    ctx.mcp_connection_service = connection_service
    return ctx


@pytest.mark.asyncio
async def test_subscribe_execute_requires_connection_service():
    """Tier 2: the op handler refuses (status='error', never raises) when
    ctx.mcp_connection_service is None — a subscription without a persistent
    connection can never observe a push, so it must not silently "succeed"."""
    from reyn.core.op_runtime.mcp_subscribe_resource import _execute

    events = EventLog()
    op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server="srv", uri=_URI)
    ctx = _make_ctx(events, resolver=None, connection_service=None)

    result = await _execute(op, ctx)
    assert result["status"] == "error"
    assert "persistent" in result["error"] or "held" in result["error"]


@pytest.mark.asyncio
async def test_subscribe_execute_real_subscribe_and_emits_events():
    """Tier 2: _execute (permission bypassed) subscribes through a REAL
    MCPConnectionService and emits mcp_resource_subscribe + _subscribed."""
    from reyn.core.op_runtime.mcp_subscribe_resource import _execute

    events = EventLog()
    service = MCPConnectionService()
    op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server="srv", uri=_URI)

    try:
        ctx = _make_ctx(events, resolver=None, connection_service=service)
        result = await _execute(op, ctx)
    finally:
        await service.aclose()

    assert result["status"] == "ok"
    assert result["uri"] == _URI
    types_seen = [e.type for e in events.all()]
    assert "mcp_resource_subscribe" in types_seen
    assert "mcp_resource_subscribed" in types_seen
    assert "mcp_resource_subscribe_failed" not in types_seen


@pytest.mark.asyncio
async def test_unsubscribe_execute_real_round_trip():
    """Tier 2: mcp_unsubscribe_resource's _execute mirrors subscribe's, after a
    real subscribe already happened."""
    from reyn.core.op_runtime.mcp_subscribe_resource import _execute as _sub_execute
    from reyn.core.op_runtime.mcp_unsubscribe_resource import _execute as _unsub_execute

    events = EventLog()
    service = MCPConnectionService()
    sub_op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server="srv", uri=_URI)
    unsub_op = MCPUnsubscribeResourceIROp(kind="mcp_unsubscribe_resource", server="srv", uri=_URI)

    try:
        ctx = _make_ctx(events, resolver=None, connection_service=service)
        await _sub_execute(sub_op, ctx)
        result = await _unsub_execute(unsub_op, ctx)
    finally:
        await service.aclose()

    assert result["status"] == "ok"
    types_seen = [e.type for e in events.all()]
    assert "mcp_resource_unsubscribed" in types_seen


def test_subscribe_handle_denies_without_permissions_mcp_declared():
    """Tier 2: require_mcp gate — status='denied' when the caller's
    PermissionDecl does not declare the server under `mcp` (mirrors
    mcp_read_resource's own test)."""
    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=Path("."), interactive=False)
    ctx = _make_ctx(events, resolver=resolver, decl=PermissionDecl())
    ctx.intervention_bus = _UnusedBus()

    op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server="srv", uri=_URI)
    result = _run(execute_op(op, ctx))

    assert result["status"] == "denied"
    denials = [e for e in events.all() if e.type == "permission_denied"]
    assert denials
    assert denials[0].data.get("kind") == "mcp_subscribe_resource"


def test_unsubscribe_handle_denies_without_permissions_mcp_declared():
    """Tier 2: same gate for mcp_unsubscribe_resource."""
    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=Path("."), interactive=False)
    ctx = _make_ctx(events, resolver=resolver, decl=PermissionDecl())
    ctx.intervention_bus = _UnusedBus()

    op = MCPUnsubscribeResourceIROp(kind="mcp_unsubscribe_resource", server="srv", uri=_URI)
    result = _run(execute_op(op, ctx))

    assert result["status"] == "denied"


def test_subscribe_execute_reports_error_for_unconfigured_server():
    """Tier 2: _execute returns status='error' (never raises) for an
    unconfigured server — mirrors mcp_read_resource's own test."""
    from reyn.core.op_runtime.mcp_subscribe_resource import _execute

    events = EventLog()
    op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server="nope", uri=_URI)
    ctx = _make_ctx(events, resolver=None, connection_service=MCPConnectionService())

    result = _run(_execute(op, ctx))
    assert result["status"] == "error"
    assert "not configured" in result["error"]


# ── Tier 2: op-kind registry completeness ─────────────────────────────────────


def test_subscribe_ops_registered_in_op_kind_model_map():
    """Tier 2: OP_KIND_MODEL_MAP <-> Op union completeness invariant — the two
    new kinds are registered (control-ir.md hard rule)."""
    from reyn.schemas.models import (
        ALL_OP_KINDS,
        OP_KIND_MODEL_MAP,
        MCPSubscribeResourceIROp,
        MCPUnsubscribeResourceIROp,
    )

    assert "mcp_subscribe_resource" in ALL_OP_KINDS
    assert "mcp_unsubscribe_resource" in ALL_OP_KINDS
    assert OP_KIND_MODEL_MAP["mcp_subscribe_resource"] is MCPSubscribeResourceIROp
    assert OP_KIND_MODEL_MAP["mcp_unsubscribe_resource"] is MCPUnsubscribeResourceIROp


# ── Tier 2: tools/mcp.py verbs — delegation via a Fake host (no mocks) ────────


class _RecordingSubscribeHost:
    """A trivial Fake at the host.mcp_subscribe_resource/mcp_unsubscribe_resource
    boundary — records calls and returns canned data (mirrors
    _RecordingResourceHost in test_2597_s2a_mcp_resources_consumption.py)."""

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple] = []
        self.unsubscribe_calls: list[tuple] = []

    async def mcp_subscribe_resource(self, server: str, uri: str):
        self.subscribe_calls.append((server, uri))
        return {"status": "ok", "server": server, "uri": uri}

    async def mcp_unsubscribe_resource(self, server: str, uri: str):
        self.unsubscribe_calls.append((server, uri))
        return {"status": "ok", "server": server, "uri": uri}


def _tool_ctx(host):
    from reyn.tools.types import RouterCallerState, ToolContext

    return ToolContext(
        caller_kind="router", events=None, permission_resolver=None, workspace=None,
        router_state=RouterCallerState(host=host),
    )


def test_subscribe_mcp_resource_handler_delegates_to_host():
    """Tier 2: _handle_subscribe_mcp_resource delegates (server, uri) to
    host.mcp_subscribe_resource and returns its result verbatim."""
    from reyn.tools.mcp import _handle_subscribe_mcp_resource

    host = _RecordingSubscribeHost()
    result = _run(
        _handle_subscribe_mcp_resource({"server": "srv", "uri": _URI}, _tool_ctx(host))
    )

    assert host.subscribe_calls == [("srv", _URI)]
    assert result == {"status": "ok", "server": "srv", "uri": _URI}


def test_unsubscribe_mcp_resource_handler_delegates_to_host():
    """Tier 2: _handle_unsubscribe_mcp_resource mirrors subscribe's delegation."""
    from reyn.tools.mcp import _handle_unsubscribe_mcp_resource

    host = _RecordingSubscribeHost()
    result = _run(
        _handle_unsubscribe_mcp_resource({"server": "srv", "uri": _URI}, _tool_ctx(host))
    )

    assert host.unsubscribe_calls == [("srv", _URI)]
    assert result == {"status": "ok", "server": "srv", "uri": _URI}


# ── Tier 2: ToolDefinition registration shape ─────────────────────────────────


def test_subscribe_tool_definitions_registered_router_allow_side_effect():
    """Tier 2: the 2 new ToolDefinitions are router+phase allow, discovery
    category, side_effect purity (subscribing mutates server-side state,
    unlike read_only list/read)."""
    from reyn.tools.mcp import SUBSCRIBE_MCP_RESOURCE, UNSUBSCRIBE_MCP_RESOURCE

    for td in (SUBSCRIBE_MCP_RESOURCE, UNSUBSCRIBE_MCP_RESOURCE):
        assert td.gates.router == "allow"
        assert td.gates.phase == "allow"
        assert td.category == "discovery"
        assert td.purity == "side_effect"


def test_subscribe_tool_definitions_present_in_default_registry():
    """Tier 2: get_default_registry() includes the 2 new capabilities under
    their canonical chat-tool names."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    assert registry.lookup("subscribe_mcp_resource") is not None
    assert registry.lookup("unsubscribe_mcp_resource") is not None
