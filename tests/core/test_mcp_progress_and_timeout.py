"""Tier 2: MCP long-running tool call — progress callback + per-call
timeout wire-up (issue #264 (a)+(b)).

Pins the contract that the MCP SDK's ``progress_callback`` and
``read_timeout_seconds`` features are forwarded end-to-end:

  1. ``MCPClient.call_tool`` accepts ``progress_callback`` /
     ``timeout_seconds`` kwargs and passes them to the official SDK's
     ``ClientSession.call_tool(progress_callback=..., read_timeout_
     seconds=...)`` directly (#4282: every transport goes through the
     official SDK now — the fastmcp-shaped ``call_tool_mcp(progress_
     handler=..., timeout=...)`` branch these fakes originally exercised
     was removed once fastmcp was no longer constructed anywhere in
     ``client.py``).
  2. ``op_runtime.mcp._execute`` builds a progress callback that emits
     ``mcp_progress`` events on the run's EventLog so subscribers can
     observe what the MCP server is doing.
  3. ``op_runtime.mcp._execute`` reads ``call_timeout_seconds`` from the
     server's raw config dict (the per-server entry under
     ``mcp.servers.<name>``) and forwards it to ``MCPClient.call_tool``.

The fakes below stand in for ``mcp.ClientSession`` (``client._client``) —
they fake ``call_tool(name, arguments, progress_callback=None,
read_timeout_seconds=None)``, the official SDK's own method signature,
which ``MCPClient.call_tool`` calls directly.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

from reyn.core.events.events import EventLog
from reyn.mcp.client import MCPClient
from reyn.schemas.models import MCPIROp
from tests._support.events import collect_events, settle


class _StubPool:
    """Test double for MCPClientPool — get() returns a pre-set client (a359 P2). Real Fake."""
    def __init__(self, client): self._client = client
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    @property
    def owner_task(self): return None
    async def get(self, server, config, *, agent_id=None): return self._client


class _FakeServerCapabilities:
    """Stand-in for ``mcp.types.ServerCapabilities`` — advertises "tools" only
    (non-None), matching the transport-level fakes in this file, which only ever
    exercise ``call_tool``. #2597 capability/version gate: the tests below
    hand-construct a half-initialised ``MCPClient`` that bypasses the real
    ``initialize()`` handshake (see each test), so ``supports("tools")`` must be
    primed the same way ``_initialized``/``_client`` are, or the gate added in
    ``MCPClient.call_tool`` fails these fakes fast for a reason unrelated to what
    they're testing (progress/timeout wiring, not the capability gate)."""
    tools: Any = object()
    resources: Any = None
    prompts: Any = None
    logging: Any = None
    completions: Any = None


def _bypass_initialize(client: MCPClient, fake_fastmcp_client: Any) -> None:
    """Prime ``client`` as if ``initialize()`` had run against a real server that
    advertises "tools" (protocol version fixed for determinism), without spawning
    a real transport. See ``_FakeServerCapabilities``."""
    client._initialized = True
    client._client = fake_fastmcp_client
    client._negotiated_version = "2025-11-25"
    client._server_capabilities = _FakeServerCapabilities()

# ── 1. MCPClient.call_tool signature accepts the new kwargs ────────────


def test_call_tool_signature_accepts_progress_callback_and_timeout() -> None:
    """Tier 2: the public surface gains keyword-only ``progress_callback`` and
    ``timeout_seconds`` parameters. issue #264 (a)+(b).
    """
    sig = inspect.signature(MCPClient.call_tool)
    params = sig.parameters
    assert "progress_callback" in params
    assert "timeout_seconds" in params
    assert params["progress_callback"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["timeout_seconds"].kind == inspect.Parameter.KEYWORD_ONLY
    # Default-None preserves pre-#264 behaviour for callers that don't pass them.
    assert params["progress_callback"].default is None
    assert params["timeout_seconds"].default is None


def test_call_tool_passes_progress_callback_and_read_timeout_to_official_sdk() -> None:
    """Tier 2: when both kwargs are set, ``MCPClient.call_tool`` forwards them
    to ``self._client.call_tool`` using the official SDK's own parameter
    names (``progress_callback`` and ``read_timeout_seconds``).

    #4412 pin-bump PR: ``read_timeout_seconds`` is ``float | None`` on
    mcp 2.0's ``ClientSession.call_tool`` (confirmed live via its own
    signature) -- was ``timedelta | None`` on 1.x. ``MCPClient.call_tool``
    now passes the raw float straight through.
    """
    captured: dict[str, Any] = {}

    class _FakeResult:
        content: list = []
        isError: bool = False
        structuredContent: Any = None
        meta: Any = None

        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _FakeFastMCPClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            captured["name"] = name
            captured["arguments"] = arguments
            captured["kwargs"] = kwargs
            return _FakeResult()

    # Hand-construct a half-initialised client that bypasses the real
    # transport. The signature accepts a config dict + sets internal
    # state directly so we don't have to spawn a subprocess.
    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    _bypass_initialize(client, _FakeFastMCPClient())

    async def _on_progress(progress: float, total: float | None, msg: str | None) -> None:
        return None

    asyncio.run(
        client.call_tool(
            "demo",
            {"x": 1},
            progress_callback=_on_progress,
            timeout_seconds=4.5,
        ),
    )

    assert captured["name"] == "demo"
    assert captured["arguments"] == {"x": 1}
    assert captured["kwargs"].get("progress_callback") is _on_progress
    read_to = captured["kwargs"].get("read_timeout_seconds")
    assert isinstance(read_to, float)
    assert read_to == 4.5


def test_call_tool_omits_kwargs_when_none_for_backwards_compat() -> None:
    """Tier 2: with default-None kwargs, neither ``progress_callback`` nor
    ``read_timeout_seconds`` is added to the SDK client call — preserves
    pre-#264 behaviour exactly so configs that omit the new keys see no
    observable change.
    """
    captured: dict[str, Any] = {}

    class _FakeResult:
        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _FakeFastMCPClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            captured["kwargs"] = kwargs
            return _FakeResult()

    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    _bypass_initialize(client, _FakeFastMCPClient())

    asyncio.run(client.call_tool("demo", {"x": 1}))

    assert "progress_callback" not in captured["kwargs"]
    assert "read_timeout_seconds" not in captured["kwargs"]


# ── 2. op_runtime.mcp emits mcp_progress events from the SDK callback ──


def test_op_handler_progress_callback_emits_mcp_progress_event() -> None:
    """Tier 2: ``_execute`` builds an ``async _on_progress`` callback that,
    when invoked by the MCP SDK, emits an ``mcp_progress`` event on the
    run's EventLog with structured fields the forwarder consumes.
    """
    from reyn.core.op_runtime import mcp as mcp_op_handler

    captured_callback: dict[str, Any] = {}

    class _FakeResult:
        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _CapturingFastMCPClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            # Capture the callback the op handler passed; FastMCP would
            # invoke this with (progress, total, message) when the server
            # sends notifications/progress.
            captured_callback["cb"] = kwargs.get("progress_callback")
            captured_callback["read_timeout_seconds"] = kwargs.get("read_timeout_seconds")
            # Simulate two progress notifications mid-call.
            cb = kwargs.get("progress_callback")
            if cb is not None:
                await cb(0.25, 1.0, "starting")
                await cb(1.0, 1.0, "done")
            return _FakeResult()

    # Build a minimal OpContext.
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    collected = collect_events(events)
    ctx = OpContext(
        workspace=None,  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        mcp_servers={"demo": {"type": "stdio", "command": "/bin/true"}},
    )
    # Pre-install a fake client so MCPClient construction is skipped.
    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    _bypass_initialize(client, _CapturingFastMCPClient())
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})

    async def _execute_and_settle() -> None:
        await mcp_op_handler._execute(op, ctx)
        await settle(events)

    asyncio.run(_execute_and_settle())

    # At least two mcp_progress events should have been emitted with the
    # structured fields the forwarder consumes.
    progress_events = [
        e.model_dump(mode="json") for e in collected
        if e.type == "mcp_progress"
    ]
    assert progress_events, "expected mcp_progress events to be emitted"
    assert any(e["data"]["message"] == "starting" for e in progress_events), "starting event missing"
    first = progress_events[0]
    assert first["data"]["server"] == "demo"
    assert first["data"]["tool"] == "thing"
    assert first["data"]["progress"] == 0.25
    assert first["data"]["total"] == 1.0
    assert first["data"]["message"] == "starting"
    last = progress_events[-1]
    assert last["data"]["progress"] == 1.0
    assert last["data"]["message"] == "done"


def test_op_handler_reads_call_timeout_from_server_config() -> None:
    """Tier 2: when ``mcp.servers.<name>.call_timeout_seconds`` is set, the
    op handler reads it from the raw config dict and forwards as
    ``timeout_seconds`` to ``MCPClient.call_tool``, which passes it as
    ``read_timeout_seconds`` to the SDK (a plain ``float`` on mcp 2.0,
    #4412 pin-bump PR -- was converted to ``timedelta`` on 1.x).
    """
    from reyn.core.op_runtime import mcp as mcp_op_handler

    captured: dict[str, Any] = {}

    class _FakeResult:
        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _CapturingFastMCPClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            captured["read_timeout_seconds"] = kwargs.get("read_timeout_seconds")
            return _FakeResult()

    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    ctx = OpContext(
        workspace=None,  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        mcp_servers={
            "demo": {
                "type": "stdio",
                "command": "/bin/true",
                "call_timeout_seconds": 7.5,
            },
        },
    )
    client = MCPClient(
        {"type": "stdio", "command": "/bin/true", "call_timeout_seconds": 7.5},
    )
    _bypass_initialize(client, _CapturingFastMCPClient())
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})
    asyncio.run(mcp_op_handler._execute(op, ctx))

    read_to = captured["read_timeout_seconds"]
    assert isinstance(read_to, float)
    assert read_to == 7.5


def test_op_handler_call_timeout_default_finite_and_optout() -> None:
    """Tier 2: #a359 S3 — a hung server must not block reyn, so the op handler forwards a FINITE
    default ``read_timeout_seconds`` when the config omits ``call_timeout_seconds`` (or sets a
    malformed value → fail-safe default). ONLY an explicit opt-out (``<= 0``) forwards no timeout
    (SDK default / unbounded). (Was: missing/invalid → unset, which let tui's slow_response=30s hang.)
    """
    from reyn.core.op_runtime import mcp as mcp_op_handler

    # (cfg, expect_timeout_forwarded): opt-out (<=0) → no read_timeout_seconds; else finite default.
    cases: list[tuple[dict[str, Any], bool]] = [
        ({"type": "stdio", "command": "/bin/true"}, True),                              # missing → default
        ({"type": "stdio", "command": "/bin/true", "call_timeout_seconds": "slow"}, True),  # invalid → fail-safe default
        ({"type": "stdio", "command": "/bin/true", "call_timeout_seconds": 0}, False),  # explicit opt-out
        ({"type": "stdio", "command": "/bin/true", "call_timeout_seconds": -1}, False),  # explicit opt-out
    ]
    for cfg, expect_forwarded in cases:
        captured: dict[str, Any] = {}

        class _FakeResult:
            def model_dump(self, mode: str = "json") -> dict:
                return {"content": [], "isError": False}

        class _CapturingFastMCPClient:
            async def call_tool(
                self,
                name: str,
                arguments: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> _FakeResult:
                captured["kwargs"] = kwargs
                return _FakeResult()

        from reyn.core.op_runtime.context import OpContext
        from reyn.security.permissions.permissions import PermissionDecl

        events = EventLog()
        ctx = OpContext(
            workspace=None,  # type: ignore[arg-type]
            events=events,
            permission_decl=PermissionDecl(),
            permission_resolver=None,
            mcp_servers={"demo": cfg},
            )
        client = MCPClient(cfg)
        _bypass_initialize(client, _CapturingFastMCPClient())
        ctx.mcp_pool = _StubPool(client)

        op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})
        asyncio.run(mcp_op_handler._execute(op, ctx))

        if expect_forwarded:
            assert "read_timeout_seconds" in captured["kwargs"], (
                f"cfg {cfg!r} should forward a FINITE default timeout (hung-server "
                f"guard), got kwargs={captured['kwargs']}"
            )
        else:
            assert "read_timeout_seconds" not in captured["kwargs"], (
                f"cfg {cfg!r} is an explicit opt-out (<=0) → NO timeout, "
                f"got kwargs={captured['kwargs']}"
            )


# ── 5. SDK-passing grep-pin (= belt-and-suspenders against future drift) ─


def test_mcp_client_call_tool_forwards_progress_and_timeout_kwargs() -> None:
    """Tier 2: source-level pin that ``MCPClient.call_tool`` constructs the
    SDK client kwargs from ``progress_callback`` / ``timeout_seconds``
    (#4282: forwarded as the official SDK's own ``progress_callback`` /
    ``read_timeout_seconds`` parameter names). Catches a future refactor
    that quietly drops the kwargs without noticing the round-trip tests
    still pass (= belt-and-suspenders for the behavioural test above).
    """
    src = inspect.getsource(MCPClient.call_tool)
    # Both kwargs must appear in the SDK kwargs builder.
    assert "progress_callback" in src
    assert "read_timeout_seconds" in src
    # #4412 pin-bump PR: read_timeout_seconds is passed as a plain float on
    # mcp 2.0 (its own signature: float | None -- confirmed live), not
    # converted to a timedelta as it was on 1.x -- this pin dropped
    # accordingly, not weakened by omission.
