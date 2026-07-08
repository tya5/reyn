"""Tests for #2597 slice ②a — MCP resources consumption (list/read + templates).

Real instances only, per the testing policy: no ``unittest.mock`` / ``MagicMock`` /
``AsyncMock`` / ``patch``. Round-trips spawn REAL MCP servers (stdio subprocess),
mirroring ``test_2597_capability_version_gate.py``:

  - ``mcp_resources_server.py`` (low-level SDK) — registers ONLY a resource
    (``resource://greeting`` -> "hello from a resource"), no tools, no templates.
    The real "server advertises resources" case, and the real "list_resource_templates
    on a server with none registered returns [], not an error" case.
  - ``mcp_paginated_tools_server.py`` (low-level SDK, tools-only) — registers NO
    resource handlers, so its negotiated ``resources`` capability is None. The real
    "server does NOT advertise resources" case for the gated fail-fast tests.

Subscribe / resources/updated (slice ②b) are out of scope — list/read only.
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
from reyn.mcp.client import MCPClient, MCPError
from reyn.mcp.gateway import MCPFault, MCPGateway
from reyn.mcp.pool import MCPClientPool
from reyn.schemas.models import MCPReadResourceIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention


class _UnusedBus(InterventionBus):
    """A real InterventionBus that fails the test if ever actually invoked — the
    non-interactive resolver paths below resolve without prompting (deny:
    ungranted decl fails before `_approve`; allow: config `mcp: allow`
    short-circuits `_approve`), so `request()` should never be called."""

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        raise AssertionError(f"intervention bus should not be consulted: {iv}")

_SUPPORT_DIR = Path(__file__).parent / "_support"
_RESOURCES_SERVER = _SUPPORT_DIR / "mcp_resources_server.py"
_TOOLS_ONLY_SERVER = _SUPPORT_DIR / "mcp_paginated_tools_server.py"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_RESOURCE_URI = "resource://greeting"
_RESOURCE_TEXT = "hello from a resource"


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


def _run(coro):
    return asyncio.run(coro)


# ── Tier 1: MCPClient — real round-trip against a real resources server ───────


def test_list_resources_returns_registered_resource():
    """Tier 1: MCPClient.list_resources() returns the one resource a real server
    registers, flattened to a plain dict with the real uri/name/mimeType."""

    async def _it():
        async with MCPClient(_stdio_cfg(_RESOURCES_SERVER)) as client:
            return await client.list_resources()

    (resource,) = _run(_it())  # unpack: exactly one entry — a server-count invariant, not a format pin
    assert resource["uri"] == _RESOURCE_URI
    assert resource["name"] == "greeting"


def test_read_resource_returns_contents():
    """Tier 1: MCPClient.read_resource(uri) returns {"contents": [...]} with the
    real server-authored text."""

    async def _it():
        async with MCPClient(_stdio_cfg(_RESOURCES_SERVER)) as client:
            return await client.read_resource(_RESOURCE_URI)

    result = _run(_it())
    (content,) = result["contents"]  # unpack: exactly one content item for this single-resource server
    assert content["text"] == _RESOURCE_TEXT


def test_list_resource_templates_empty_not_error_when_none_registered():
    """Tier 1: a real server that registers no resource templates returns an empty
    list (a normal result), not an MCPError. Uses the FastMCP echo server — verified
    empirically (test_2597_capability_version_gate.py) that a FastMCP-built server
    implements the ``resources/templates/list`` handler for every server regardless
    of what it registers, so it answers "[]" rather than "Method not found" (unlike
    the low-level SDK ``mcp_resources_server.py``, which has no template handler at
    all and genuinely raises — that server exists to prove capability ADVERTISING,
    not template-listing behavior)."""

    async def _it():
        async with MCPClient(_stdio_cfg(_ECHO_SERVER)) as client:
            return await client.list_resource_templates()

    templates = _run(_it())
    assert templates == []


def test_list_resources_fails_fast_against_server_without_resources_capability():
    """Tier 1: the #2597 capability gate blocks list_resources against a real
    server that never advertised "resources" — a clear MCPError, not a raw
    protocol error or a silent empty list."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_TOOLS_ONLY_SERVER), server_name="tools-only-srv"
        ) as client:
            await client.list_resources()

    with pytest.raises(MCPError) as exc_info:
        _run(_it())
    message = str(exc_info.value)
    assert "resources" in message
    assert "tools-only-srv" in message


def test_read_resource_fails_fast_against_server_without_resources_capability():
    """Tier 1: same gate for read_resource — the content-returning call fails
    fast rather than reaching (and confusing) the server."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_TOOLS_ONLY_SERVER), server_name="tools-only-srv"
        ) as client:
            await client.read_resource("resource://anything")

    with pytest.raises(MCPError) as exc_info:
        _run(_it())
    message = str(exc_info.value)
    assert "resources" in message
    assert "tools-only-srv" in message


# ── Tier 1: MCPGateway — resources pass-throughs ──────────────────────────────


def test_gateway_list_resources_round_trip():
    """Tier 1: MCPGateway.list_resources opens + lists + tears down through the
    same fault-contained seam as list_tools."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.list_resources("resources-srv", _stdio_cfg(_RESOURCES_SERVER))

    resources = _run(_it())
    assert resources[0]["uri"] == _RESOURCE_URI


def test_gateway_read_resource_round_trip():
    """Tier 1: MCPGateway.read_resource returns the flattened contents."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.read_resource(
            "resources-srv", _RESOURCE_URI, _stdio_cfg(_RESOURCES_SERVER)
        )

    result = _run(_it())
    assert result["contents"][0]["text"] == _RESOURCE_TEXT


def test_gateway_read_resource_raises_mcp_fault_on_ungated_server():
    """Tier 1: the gateway surfaces the capability-gate failure as MCPFault (its
    ONE contained-fault type), never a bare MCPError/protocol exception."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.read_resource(
            "tools-only", "resource://x", _stdio_cfg(_TOOLS_ONLY_SERVER)
        )

    with pytest.raises(MCPFault):
        _run(_it())


# ── Tier 2: held connection (non-ephemeral session path) — the PRODUCTION path ─
# The live (non-ephemeral) session routes resource reads through a held-open
# MCPConnectionService (Option C), NOT the one-shot MCPGateway() pool the tests
# above exercise. That path hands the gateway a _HeldConnection (not a bare
# MCPClient), so read_resource/list_resources must exist ON THE HELD HANDLE or the
# call AttributeErrors. These tests route through a REAL MCPConnectionService — the
# coverage the one-shot-pool tests structurally cannot give.


@pytest.mark.asyncio
async def test_held_connection_reads_resource_and_reuses_subprocess():
    """Tier 2: the CORE held-path coverage — a resource read through a REAL
    MCPConnectionService (via MCPGateway(pool=service), exactly as the live session
    wires it) works, and a 2nd read hits the SAME held subprocess. Proven via the
    echo server's ``resource://pid`` (content = the server PID): identical PID across
    two reads = the held connection was reused, no re-handshake. This is the exact
    production path #2605-review flagged as crashing (AttributeError) and untested."""
    from reyn.mcp.connection_service import MCPConnectionService

    service = MCPConnectionService()
    try:
        gateway = MCPGateway(pool=service)
        r1 = await gateway.read_resource("srv", "resource://pid", _stdio_cfg(_ECHO_SERVER))
        r2 = await gateway.read_resource("srv", "resource://pid", _stdio_cfg(_ECHO_SERVER))
        pid_1 = r1["contents"][0]["text"]
        pid_2 = r2["contents"][0]["text"]
        assert pid_1 and pid_1 == pid_2, "same held subprocess reused across reads (no re-handshake)"
        assert service.held_servers() == ["srv"], "the connection is held open, not opened+closed per read"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_held_connection_lists_resources_and_templates():
    """Tier 2: list_resources + list_resource_templates on the HELD handle
    (_HeldConnection) — both must exist there (not just on the one-shot MCPClient),
    routed through the same MCPConnectionService the live session uses."""
    from reyn.mcp.connection_service import MCPConnectionService

    service = MCPConnectionService()
    try:
        gateway = MCPGateway(pool=service)
        resources = await gateway.list_resources("srv", _stdio_cfg(_ECHO_SERVER))
        templates = await gateway.list_resource_templates("srv", _stdio_cfg(_ECHO_SERVER))
        uris = [r["uri"] for r in resources]
        assert "resource://pid" in uris, "the echo server's pid resource is listed via the held handle"
        assert isinstance(templates, list), "list_resource_templates returns a list via the held handle"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_execute_reads_via_connection_service_not_just_pool():
    """Tier 2: the op handler's _execute reads through ctx.mcp_connection_service
    (the non-ephemeral session's wiring) — NOT only ctx.mcp_pool. Mirrors the
    ``_mcp_read_resource`` non-ephemeral branch (session.py) which sets
    ctx.mcp_connection_service, the path the crash lived on."""
    from reyn.core.op_runtime.mcp_read_resource import _execute
    from reyn.mcp.connection_service import MCPConnectionService

    events = EventLog()
    service = MCPConnectionService()
    op = MCPReadResourceIROp(kind="mcp_read_resource", server="srv", uri="resource://pid")

    try:
        ctx = OpContext(
            workspace=Workspace(events=events),
            events=events,
            permission_decl=PermissionDecl(),
            permission_resolver=None,  # permission bypassed — this asserts the transport wiring
            mcp_servers={"srv": _stdio_cfg(_ECHO_SERVER)},
            actor="chat_router",
        )
        ctx.mcp_connection_service = service  # the non-ephemeral wiring (not mcp_pool)
        result = await _execute(op, ctx)
    finally:
        await service.aclose()

    assert result["status"] == "ok"
    assert result["contents"][0]["text"], "resource content read through the held connection service"


# ── Tier 2: op_runtime mcp_read_resource — permission gate + events ───────────


def _make_ctx(
    tmp_path: Path,
    events: EventLog,
    *,
    resolver: "PermissionResolver | None",
    decl: "PermissionDecl | None" = None,
) -> OpContext:
    return OpContext(
        workspace=Workspace(events=events),
        events=events,
        permission_decl=decl or PermissionDecl(),
        permission_resolver=resolver,
        mcp_servers={"resources-srv": _stdio_cfg(_RESOURCES_SERVER)},
        actor="chat_router",
    )


def test_execute_reads_real_resource_and_emits_events(tmp_path):
    """Tier 2: the op handler's _execute (permission bypassed — mirrors the
    existing mcp.py _execute test pattern) reads a REAL resource through a REAL
    MCPClientPool and emits mcp_resource_read + mcp_resource_read_completed."""
    from reyn.core.op_runtime.mcp_read_resource import _execute

    events = EventLog()
    op = MCPReadResourceIROp(kind="mcp_read_resource", server="resources-srv", uri=_RESOURCE_URI)

    async def _it():
        ctx = _make_ctx(tmp_path, events, resolver=None)
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await _execute(op, ctx)

    result = _run(_it())
    assert result["status"] == "ok"
    assert result["contents"][0]["text"] == _RESOURCE_TEXT

    types_seen = [e.type for e in events.all()]
    assert "mcp_resource_read" in types_seen
    assert "mcp_resource_read_completed" in types_seen
    assert "mcp_resource_read_failed" not in types_seen


def test_handle_denies_without_permissions_mcp_declared(tmp_path):
    """Tier 2: P6/P5-style invariant — execute_op on mcp_read_resource denies
    (status='denied' + permission_denied event) when the caller's PermissionDecl
    does not declare the server under `mcp`, mirroring the `mcp` (call_tool) op's
    own require_mcp gate exactly."""
    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    # No mcp=[...] on the decl — the AgentLayer grant is empty.
    ctx = _make_ctx(tmp_path, events, resolver=resolver, decl=PermissionDecl())
    ctx.intervention_bus = _UnusedBus()  # never consulted: denied before _approve

    op = MCPReadResourceIROp(kind="mcp_read_resource", server="resources-srv", uri=_RESOURCE_URI)
    result = _run(execute_op(op, ctx))

    assert result["status"] == "denied"
    denials = [e for e in events.all() if e.type == "permission_denied"]
    assert denials, f"expected permission_denied event; got {[e.type for e in events.all()]}"
    assert denials[0].data.get("kind") == "mcp_read_resource"


def test_handle_allows_and_reads_when_permissions_mcp_granted(tmp_path):
    """Tier 2: the allow path — decl.mcp includes the server + config grants
    `mcp: allow`, so require_mcp passes and the REAL resource is read end-to-end
    through execute_op (permission gate -> gateway -> real subprocess)."""
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"mcp": "allow"}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(mcp=["resources-srv"])
    ctx = _make_ctx(tmp_path, events, resolver=resolver, decl=decl)
    ctx.intervention_bus = _UnusedBus()  # never consulted: config-approved short-circuits _approve

    op = MCPReadResourceIROp(kind="mcp_read_resource", server="resources-srv", uri=_RESOURCE_URI)

    async def _it():
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await execute_op(op, ctx)

    result = _run(_it())
    assert result["status"] == "ok"
    assert result["contents"][0]["text"] == _RESOURCE_TEXT
    assert not [e for e in events.all() if e.type == "permission_denied"]


def test_execute_reports_error_for_unconfigured_server(tmp_path):
    """Tier 2: _execute returns a clean status='error' (never raises) when the
    op names a server absent from ctx.mcp_servers — the same not-configured
    shape the `mcp` op returns."""
    from reyn.core.op_runtime.mcp_read_resource import _execute

    events = EventLog()
    op = MCPReadResourceIROp(kind="mcp_read_resource", server="nope", uri="resource://x")
    ctx = _make_ctx(tmp_path, events, resolver=None)

    result = _run(_execute(op, ctx))
    assert result["status"] == "error"
    assert "not configured" in result["error"]


# ── Tier 2: op-kind registry completeness ─────────────────────────────────────


def test_mcp_read_resource_registered_in_op_kind_model_map():
    """Tier 2: OP_KIND_MODEL_MAP <-> Op union completeness invariant — the new
    kind is registered (control-ir.md hard rule)."""
    from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, MCPReadResourceIROp

    assert "mcp_read_resource" in ALL_OP_KINDS
    assert OP_KIND_MODEL_MAP["mcp_read_resource"] is MCPReadResourceIROp


# ── Tier 2: canonical offload mapping (large blob does not double-oversize) ───


def test_canonical_mapper_joins_text_and_isolates_blob_as_structured():
    """Tier 2: #2425-style offload safety — _mcp_read_resource_to_canonical joins
    text contents into `text` (the sole offload payload) and keeps a `blob`
    content item as a `structured` attachment, never a second text-competing
    field (the exact shape of bug the tool-call canonical mapper already fixed)."""
    from reyn.core.offload.canonical import to_canonical

    result = {
        "kind": "mcp_read_resource",
        "status": "ok",
        "server": "resources-srv",
        "uri": _RESOURCE_URI,
        "contents": [
            {"uri": _RESOURCE_URI, "mimeType": "text/plain", "text": "hello"},
            {"uri": "resource://blob", "mimeType": "application/octet-stream", "blob": "YWJj"},
        ],
    }
    canonical = to_canonical(result)
    assert canonical["text"] == "hello"
    assert canonical["attachments"] == [
        {"kind": "structured", "data": {
            "uri": "resource://blob", "mimeType": "application/octet-stream", "blob": "YWJj",
        }},
    ]
    # #2425 案B: meta is signal-only — transport echo (kind/status/server/uri) is dropped; a
    # successful result carries no isError, so meta is empty.
    assert "kind" not in canonical["meta"] and "server" not in canonical["meta"]
    assert not canonical["meta"].get("isError")


# ── Tier 2: tools/mcp.py verbs — delegation via a Fake host (no mocks) ────────


class _RecordingResourceHost:
    """A trivial Fake at the host.mcp_list_resources/mcp_read_resource boundary —
    records calls and returns canned data (mirrors _RecordingMCPHost in
    test_mcp_invoke_action_tool_args_1646.py)."""

    def __init__(self, *, resources=None, templates=None, read_result=None) -> None:
        self.resources = resources if resources is not None else [
            {"uri": _RESOURCE_URI, "name": "greeting"}
        ]
        self.templates = templates if templates is not None else []
        self.read_result = read_result if read_result is not None else {
            "status": "ok", "contents": [{"uri": _RESOURCE_URI, "text": _RESOURCE_TEXT}],
        }
        self.list_calls: list[str] = []
        self.template_calls: list[str] = []
        self.read_calls: list[tuple] = []

    async def mcp_list_resources(self, server: str):
        self.list_calls.append(server)
        return self.resources

    async def mcp_list_resource_templates(self, server: str):
        self.template_calls.append(server)
        return self.templates

    async def mcp_read_resource(self, server: str, uri: str):
        self.read_calls.append((server, uri))
        return self.read_result


def _tool_ctx(host):
    from reyn.tools.types import RouterCallerState, ToolContext

    return ToolContext(
        caller_kind="router", events=None, permission_resolver=None, workspace=None,
        router_state=RouterCallerState(host=host),
    )


def test_list_mcp_resources_handler_delegates_to_host():
    """Tier 2: _handle_list_mcp_resources delegates to host.mcp_list_resources
    and wraps the result under "resources" (mirrors _handle_list_mcp_tools)."""
    from reyn.tools.mcp import _handle_list_mcp_resources

    host = _RecordingResourceHost()
    result = _run(_handle_list_mcp_resources({"server": "resources-srv"}, _tool_ctx(host)))

    assert host.list_calls == ["resources-srv"]
    assert result["resources"] == host.resources


def test_list_mcp_resource_templates_handler_delegates_to_host():
    """Tier 2: _handle_list_mcp_resource_templates delegates + wraps under
    "resource_templates"; empty list is a normal (non-error) pass-through."""
    from reyn.tools.mcp import _handle_list_mcp_resource_templates

    host = _RecordingResourceHost(templates=[])
    result = _run(
        _handle_list_mcp_resource_templates({"server": "resources-srv"}, _tool_ctx(host))
    )

    assert host.template_calls == ["resources-srv"]
    assert result["resource_templates"] == []


def test_read_mcp_resource_handler_delegates_to_host():
    """Tier 2: _handle_read_mcp_resource delegates (server, uri) to
    host.mcp_read_resource and returns its result verbatim (gating already
    happened upstream at the op-kind permission layer, not here)."""
    from reyn.tools.mcp import _handle_read_mcp_resource

    host = _RecordingResourceHost()
    result = _run(
        _handle_read_mcp_resource({"server": "resources-srv", "uri": _RESOURCE_URI}, _tool_ctx(host))
    )

    assert host.read_calls == [("resources-srv", _RESOURCE_URI)]
    assert result == host.read_result


def test_list_mcp_resources_handler_surfaces_host_error():
    """Tier 2: an [{"error": ...}] from the host (e.g. server unreachable)
    surfaces as a top-level {"error": ...}, not wrapped under "resources"
    (mirrors _handle_list_mcp_tools' error-surfacing branch)."""
    from reyn.tools.mcp import _handle_list_mcp_resources

    host = _RecordingResourceHost(resources=[{"error": "MCP server 'x' not configured"}])
    result = _run(_handle_list_mcp_resources({"server": "x"}, _tool_ctx(host)))

    assert result == {"error": "MCP server 'x' not configured"}


# ── Tier 2: ToolDefinition registration shape ─────────────────────────────────


def test_new_tool_definitions_registered_router_and_phase_allow():
    """Tier 2: the 3 new ToolDefinitions are router+phase allow, discovery
    category, and read_only purity (mirrors LIST_MCP_TOOLS)."""
    from reyn.tools.mcp import LIST_MCP_RESOURCE_TEMPLATES, LIST_MCP_RESOURCES, READ_MCP_RESOURCE

    for td in (LIST_MCP_RESOURCES, LIST_MCP_RESOURCE_TEMPLATES, READ_MCP_RESOURCE):
        assert td.gates.router == "allow"
        assert td.gates.phase == "allow"
        assert td.category == "discovery"
        assert td.purity == "read_only"


def test_new_tool_definitions_present_in_default_registry():
    """Tier 2: get_default_registry() includes the 3 new capabilities under
    their canonical chat-tool names."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    assert registry.lookup("list_mcp_resources") is not None
    assert registry.lookup("list_mcp_resource_templates") is not None
    assert registry.lookup("read_mcp_resource") is not None
