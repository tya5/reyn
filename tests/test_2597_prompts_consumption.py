"""Tests for #2597 slice ②c — MCP prompts consumption (list/get).

Real instances only, per the testing policy: no ``unittest.mock`` / ``MagicMock`` /
``AsyncMock`` / ``patch``. Round-trips spawn REAL MCP servers (stdio subprocess),
mirroring ``test_2597_s2a_mcp_resources_consumption.py``:

  - ``mcp_prompts_server.py`` (low-level SDK) — registers ONLY a prompt
    (``greeting`` -> "hello from a prompt"), no tools, no resources. The real
    "server advertises prompts" case.
  - ``mcp_paginated_tools_server.py`` (low-level SDK, tools-only) — registers NO
    prompt handlers, so its negotiated ``prompts`` capability is None. The real
    "server does NOT advertise prompts" case for the gated fail-fast tests.

Prompts have no subscribe concept — out of scope entirely, unlike resources.
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
from reyn.schemas.models import MCPGetPromptIROp
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
_PROMPTS_SERVER = _SUPPORT_DIR / "mcp_prompts_server.py"
_TOOLS_ONLY_SERVER = _SUPPORT_DIR / "mcp_paginated_tools_server.py"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_PROMPT_NAME = "greeting"
_PROMPT_TEXT = "hello from a prompt"


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


def _run(coro):
    return asyncio.run(coro)


# ── Tier 1: MCPClient — real round-trip against a real prompts server ─────────


def test_list_prompts_returns_registered_prompt():
    """Tier 1: MCPClient.list_prompts() returns the one prompt a real server
    registers, flattened to a plain dict with the real name/description/arguments."""

    async def _it():
        async with MCPClient(_stdio_cfg(_PROMPTS_SERVER)) as client:
            return await client.list_prompts()

    (prompt,) = _run(_it())  # unpack: exactly one entry — a server-count invariant, not a format pin
    assert prompt["name"] == _PROMPT_NAME
    assert prompt["description"] == "A simple greeting prompt"
    assert prompt["arguments"][0]["name"] == "style"


def test_get_prompt_returns_rendered_messages():
    """Tier 1: MCPClient.get_prompt(name) returns {"description", "messages"}
    with the real server-rendered text."""

    async def _it():
        async with MCPClient(_stdio_cfg(_PROMPTS_SERVER)) as client:
            return await client.get_prompt(_PROMPT_NAME)

    result = _run(_it())
    assert result["description"] == "A simple greeting prompt"
    (message,) = result["messages"]  # unpack: exactly one message for this single-prompt server
    assert message["content"]["text"] == _PROMPT_TEXT


def test_list_prompts_fails_fast_against_server_without_prompts_capability():
    """Tier 1: the #2597 capability gate blocks list_prompts against a real
    server that never advertised "prompts" — a clear MCPError, not a raw
    protocol error or a silent empty list."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_TOOLS_ONLY_SERVER), server_name="tools-only-srv"
        ) as client:
            await client.list_prompts()

    with pytest.raises(MCPError) as exc_info:
        _run(_it())
    message = str(exc_info.value)
    assert "prompts" in message
    assert "tools-only-srv" in message


def test_get_prompt_fails_fast_against_server_without_prompts_capability():
    """Tier 1: same gate for get_prompt — the content-returning call fails
    fast rather than reaching (and confusing) the server."""

    async def _it():
        async with MCPClient(
            _stdio_cfg(_TOOLS_ONLY_SERVER), server_name="tools-only-srv"
        ) as client:
            await client.get_prompt("anything")

    with pytest.raises(MCPError) as exc_info:
        _run(_it())
    message = str(exc_info.value)
    assert "prompts" in message
    assert "tools-only-srv" in message


# ── Tier 1: MCPGateway — prompts pass-throughs ─────────────────────────────────


def test_gateway_list_prompts_round_trip():
    """Tier 1: MCPGateway.list_prompts opens + lists + tears down through the
    same fault-contained seam as list_resources/list_tools."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.list_prompts("prompts-srv", _stdio_cfg(_PROMPTS_SERVER))

    prompts = _run(_it())
    assert prompts[0]["name"] == _PROMPT_NAME


def test_gateway_get_prompt_round_trip():
    """Tier 1: MCPGateway.get_prompt returns the flattened messages."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.get_prompt(
            "prompts-srv", _PROMPT_NAME, {}, _stdio_cfg(_PROMPTS_SERVER)
        )

    result = _run(_it())
    assert result["messages"][0]["content"]["text"] == _PROMPT_TEXT


def test_gateway_get_prompt_raises_mcp_fault_on_ungated_server():
    """Tier 1: the gateway surfaces the capability-gate failure as MCPFault (its
    ONE contained-fault type), never a bare MCPError/protocol exception."""
    gateway = MCPGateway()

    async def _it():
        return await gateway.get_prompt(
            "tools-only", "x", {}, _stdio_cfg(_TOOLS_ONLY_SERVER)
        )

    with pytest.raises(MCPFault):
        _run(_it())


# ── Tier 2: held connection (non-ephemeral session path) — the PRODUCTION path ─
# The live (non-ephemeral) session routes prompt gets through a held-open
# MCPConnectionService (Option C), NOT the one-shot MCPGateway() pool the tests
# above exercise. That path hands the gateway a _HeldConnection (not a bare
# MCPClient), so get_prompt/list_prompts must exist ON THE HELD HANDLE or the
# call AttributeErrors (the exact #2605-review gap ②a hit) — these tests route
# through a REAL MCPConnectionService — the coverage the one-shot-pool tests
# structurally cannot give.


@pytest.mark.asyncio
async def test_held_connection_gets_prompt_and_reuses_subprocess():
    """Tier 2: the CORE held-path coverage — a prompt get through a REAL
    MCPConnectionService (via MCPGateway(pool=service), exactly as the live session
    wires it) works, and a 2nd get hits the SAME held subprocess. Proven via the
    echo server's ``pid_prompt`` (rendered text = the server PID): identical PID
    across two gets = the held connection was reused, no re-handshake."""
    from reyn.mcp.connection_service import MCPConnectionService

    service = MCPConnectionService()
    try:
        gateway = MCPGateway(pool=service)
        r1 = await gateway.get_prompt("srv", "pid_prompt", {}, _stdio_cfg(_ECHO_SERVER))
        r2 = await gateway.get_prompt("srv", "pid_prompt", {}, _stdio_cfg(_ECHO_SERVER))
        pid_1 = r1["messages"][0]["content"]["text"]
        pid_2 = r2["messages"][0]["content"]["text"]
        assert pid_1 and pid_1 == pid_2, "same held subprocess reused across gets (no re-handshake)"
        assert service.held_servers() == ["srv"], "the connection is held open, not opened+closed per get"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_held_connection_lists_prompts():
    """Tier 2: list_prompts on the HELD handle (_HeldConnection) — must exist
    there (not just on the one-shot MCPClient), routed through the same
    MCPConnectionService the live session uses."""
    from reyn.mcp.connection_service import MCPConnectionService

    service = MCPConnectionService()
    try:
        gateway = MCPGateway(pool=service)
        prompts = await gateway.list_prompts("srv", _stdio_cfg(_ECHO_SERVER))
        names = [p["name"] for p in prompts]
        assert "pid_prompt" in names, "the echo server's pid prompt is listed via the held handle"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_execute_gets_via_connection_service_not_just_pool():
    """Tier 2: the op handler's _execute gets through ctx.mcp_connection_service
    (the non-ephemeral session's wiring) — NOT only ctx.mcp_pool. Mirrors the
    ``_mcp_get_prompt`` non-ephemeral branch (session.py) which sets
    ctx.mcp_connection_service, the path the ②a resources crash lived on."""
    from reyn.core.op_runtime.mcp_get_prompt import _execute
    from reyn.mcp.connection_service import MCPConnectionService

    events = EventLog()
    service = MCPConnectionService()
    op = MCPGetPromptIROp(kind="mcp_get_prompt", server="srv", name="pid_prompt", arguments={})

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
    assert result["messages"][0]["content"]["text"], "prompt content read through the held connection service"


# ── Tier 2: op_runtime mcp_get_prompt — permission gate + events ──────────────


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
        mcp_servers={"prompts-srv": _stdio_cfg(_PROMPTS_SERVER)},
        actor="chat_router",
    )


def test_execute_gets_real_prompt_and_emits_events(tmp_path):
    """Tier 2: the op handler's _execute (permission bypassed — mirrors the
    existing mcp_read_resource.py _execute test pattern) gets a REAL prompt
    through a REAL MCPClientPool and emits mcp_prompt_get + mcp_prompt_get_completed."""
    from reyn.core.op_runtime.mcp_get_prompt import _execute

    events = EventLog()
    op = MCPGetPromptIROp(kind="mcp_get_prompt", server="prompts-srv", name=_PROMPT_NAME, arguments={})

    async def _it():
        ctx = _make_ctx(tmp_path, events, resolver=None)
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await _execute(op, ctx)

    result = _run(_it())
    assert result["status"] == "ok"
    assert result["messages"][0]["content"]["text"] == _PROMPT_TEXT

    types_seen = [e.type for e in events.all()]
    assert "mcp_prompt_get" in types_seen
    assert "mcp_prompt_get_completed" in types_seen
    assert "mcp_prompt_get_failed" not in types_seen


def test_handle_denies_without_permissions_mcp_declared(tmp_path):
    """Tier 2: P6/P5-style invariant — execute_op on mcp_get_prompt denies
    (status='denied' + permission_denied event) when the caller's PermissionDecl
    does not declare the server under `mcp`, mirroring the `mcp_read_resource`
    op's own require_mcp gate exactly."""
    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    # No mcp=[...] on the decl — the AgentLayer grant is empty.
    ctx = _make_ctx(tmp_path, events, resolver=resolver, decl=PermissionDecl())
    ctx.intervention_bus = _UnusedBus()  # never consulted: denied before _approve

    op = MCPGetPromptIROp(kind="mcp_get_prompt", server="prompts-srv", name=_PROMPT_NAME, arguments={})
    result = _run(execute_op(op, ctx))

    assert result["status"] == "denied"
    denials = [e for e in events.all() if e.type == "permission_denied"]
    assert denials, f"expected permission_denied event; got {[e.type for e in events.all()]}"
    assert denials[0].data.get("kind") == "mcp_get_prompt"


def test_handle_allows_and_gets_when_permissions_mcp_granted(tmp_path):
    """Tier 2: the allow path — decl.mcp includes the server + config grants
    `mcp: allow`, so require_mcp passes and the REAL prompt is fetched end-to-end
    through execute_op (permission gate -> gateway -> real subprocess)."""
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"mcp": "allow"}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(mcp=["prompts-srv"])
    ctx = _make_ctx(tmp_path, events, resolver=resolver, decl=decl)
    ctx.intervention_bus = _UnusedBus()  # never consulted: config-approved short-circuits _approve

    op = MCPGetPromptIROp(kind="mcp_get_prompt", server="prompts-srv", name=_PROMPT_NAME, arguments={})

    async def _it():
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await execute_op(op, ctx)

    result = _run(_it())
    assert result["status"] == "ok"
    assert result["messages"][0]["content"]["text"] == _PROMPT_TEXT
    assert not [e for e in events.all() if e.type == "permission_denied"]


def test_execute_reports_error_for_unconfigured_server(tmp_path):
    """Tier 2: _execute returns a clean status='error' (never raises) when the
    op names a server absent from ctx.mcp_servers — the same not-configured
    shape the `mcp_read_resource` op returns."""
    from reyn.core.op_runtime.mcp_get_prompt import _execute

    events = EventLog()
    op = MCPGetPromptIROp(kind="mcp_get_prompt", server="nope", name="x", arguments={})
    ctx = _make_ctx(tmp_path, events, resolver=None)

    result = _run(_execute(op, ctx))
    assert result["status"] == "error"
    assert "not configured" in result["error"]


# ── Tier 2: op-kind registry completeness ─────────────────────────────────────


def test_mcp_get_prompt_registered_in_op_kind_model_map():
    """Tier 2: OP_KIND_MODEL_MAP <-> Op union completeness invariant — the new
    kind is registered (control-ir.md hard rule)."""
    from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, MCPGetPromptIROp

    assert "mcp_get_prompt" in ALL_OP_KINDS
    assert OP_KIND_MODEL_MAP["mcp_get_prompt"] is MCPGetPromptIROp


# ── Tier 2: canonical offload mapping (a large non-text block does not double-oversize) ─


def test_canonical_mapper_joins_text_and_isolates_non_text_as_structured():
    """Tier 2: #2425-style offload safety — _mcp_get_prompt_to_canonical joins
    text content into `text` (the sole offload payload) and keeps a non-text
    content block as a `structured` attachment, never a second text-competing
    field (the exact shape of bug the tool-call / resource-read canonical
    mappers already fixed)."""
    from reyn.core.offload.canonical import to_canonical

    result = {
        "kind": "mcp_get_prompt",
        "status": "ok",
        "server": "prompts-srv",
        "name": _PROMPT_NAME,
        "description": "A simple greeting prompt",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "hello"}},
            {"role": "user", "content": {"type": "image", "data": "YWJj", "mimeType": "image/png"}},
        ],
    }
    canonical = to_canonical(result)
    assert canonical["text"] == "hello"
    assert canonical["attachments"] == [
        {"kind": "structured", "data": {"type": "image", "data": "YWJj", "mimeType": "image/png"}},
    ]
    # #2425 案B: meta is signal-only — transport echo (kind/status/server/name) is dropped; a
    # successful result carries no isError, so meta is empty.
    assert "kind" not in canonical["meta"] and "server" not in canonical["meta"]
    assert not canonical["meta"].get("isError")


# ── Tier 2: tools/mcp.py verbs — delegation via a Fake host (no mocks) ────────


class _RecordingPromptHost:
    """A trivial Fake at the host.mcp_list_prompts/mcp_get_prompt boundary —
    records calls and returns canned data (mirrors _RecordingResourceHost in
    test_2597_s2a_mcp_resources_consumption.py)."""

    def __init__(self, *, prompts=None, get_result=None) -> None:
        self.prompts = prompts if prompts is not None else [
            {"name": _PROMPT_NAME, "description": "A simple greeting prompt", "arguments": []}
        ]
        self.get_result = get_result if get_result is not None else {
            "status": "ok",
            "messages": [{"role": "user", "content": {"type": "text", "text": _PROMPT_TEXT}}],
        }
        self.list_calls: list[str] = []
        self.get_calls: list[tuple] = []

    async def mcp_list_prompts(self, server: str):
        self.list_calls.append(server)
        return self.prompts

    async def mcp_get_prompt(self, server: str, name: str, arguments=None):
        self.get_calls.append((server, name, arguments))
        return self.get_result


def _tool_ctx(host):
    from reyn.tools.types import RouterCallerState, ToolContext

    return ToolContext(
        caller_kind="router", events=None, permission_resolver=None, workspace=None,
        router_state=RouterCallerState(host=host),
    )


def test_list_mcp_prompts_handler_delegates_to_host():
    """Tier 2: _handle_list_mcp_prompts delegates to host.mcp_list_prompts
    and wraps the result under "prompts" (mirrors _handle_list_mcp_resources)."""
    from reyn.tools.mcp import _handle_list_mcp_prompts

    host = _RecordingPromptHost()
    result = _run(_handle_list_mcp_prompts({"server": "prompts-srv"}, _tool_ctx(host)))

    assert host.list_calls == ["prompts-srv"]
    assert result["prompts"] == host.prompts


def test_get_mcp_prompt_handler_delegates_to_host():
    """Tier 2: _handle_get_mcp_prompt delegates (server, name, arguments) to
    host.mcp_get_prompt and returns its result verbatim (gating already
    happened upstream at the op-kind permission layer, not here)."""
    from reyn.tools.mcp import _handle_get_mcp_prompt

    host = _RecordingPromptHost()
    result = _run(
        _handle_get_mcp_prompt(
            {"server": "prompts-srv", "name": _PROMPT_NAME, "arguments": {"style": "brief"}},
            _tool_ctx(host),
        )
    )

    assert host.get_calls == [("prompts-srv", _PROMPT_NAME, {"style": "brief"})]
    assert result == host.get_result


def test_list_mcp_prompts_handler_surfaces_host_error():
    """Tier 2: an [{"error": ...}] from the host (e.g. server unreachable)
    surfaces as a top-level {"error": ...}, not wrapped under "prompts"
    (mirrors _handle_list_mcp_resources' error-surfacing branch)."""
    from reyn.tools.mcp import _handle_list_mcp_prompts

    host = _RecordingPromptHost(prompts=[{"error": "MCP server 'x' not configured"}])
    result = _run(_handle_list_mcp_prompts({"server": "x"}, _tool_ctx(host)))

    assert result == {"error": "MCP server 'x' not configured"}


# ── Tier 2: ToolDefinition registration shape ─────────────────────────────────


def test_new_tool_definitions_registered_router_and_phase_allow():
    """Tier 2: the 2 new ToolDefinitions are router+phase allow, discovery
    category, and read_only purity (mirrors LIST_MCP_RESOURCES/READ_MCP_RESOURCE)."""
    from reyn.tools.mcp import GET_MCP_PROMPT, LIST_MCP_PROMPTS

    for td in (LIST_MCP_PROMPTS, GET_MCP_PROMPT):
        assert td.gates.router == "allow"
        assert td.gates.phase == "allow"
        assert td.category == "discovery"
        assert td.purity == "read_only"


def test_new_tool_definitions_present_in_default_registry():
    """Tier 2: get_default_registry() includes the 2 new capabilities under
    their canonical chat-tool names."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    assert registry.lookup("list_mcp_prompts") is not None
    assert registry.lookup("get_mcp_prompt") is not None
