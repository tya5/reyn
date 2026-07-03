"""Tier 2: MCP long-running tool call — progress callback + per-call
timeout wire-up (issue #264 (a)+(b)).

Pins the contract that the MCP SDK's ``progress_callback`` and
``read_timeout_seconds`` features — which were present at the SDK
level but unused by the Reyn integration before this PR — are now
forwarded end-to-end:

  1. ``MCPClient.call_tool`` accepts ``progress_callback`` /
     ``timeout_seconds`` kwargs and passes them to the SDK session.
  2. ``op_runtime.mcp._execute`` builds a progress callback that emits
     ``mcp_progress`` events on the run's EventLog so subscribers can
     observe what the MCP server is doing.
  3. ``op_runtime.mcp._execute`` reads ``call_timeout_seconds`` from the
     server's raw config dict (the per-server entry under
     ``mcp.servers.<name>``) and forwards it to ``MCPClient.call_tool``.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.mcp.client import MCPClient
from reyn.schemas.models import MCPIROp


class _StubPool:
    """Test double for MCPClientPool — get() returns a pre-set client (a359 P2). Real Fake."""
    def __init__(self, client): self._client = client
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    @property
    def owner_task(self): return None
    async def get(self, server, config, *, agent_id=None): return self._client

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


def test_call_tool_passes_progress_callback_and_timedelta_to_sdk_session() -> None:
    """Tier 2: when both kwargs are set, ``MCPClient.call_tool`` forwards them
    to ``self._session.call_tool`` using the SDK's parameter names
    (``progress_callback`` and ``read_timeout_seconds`` as a ``timedelta``).
    """
    captured: dict[str, Any] = {}

    class _FakeResult:
        content: list = []
        isError: bool = False
        structuredContent: Any = None
        meta: Any = None

        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _FakeSession:
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
    client._initialized = True
    client._session = _FakeSession()

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
    assert isinstance(read_to, timedelta)
    assert read_to == timedelta(seconds=4.5)


def test_call_tool_omits_kwargs_when_none_for_backwards_compat() -> None:
    """Tier 2: with default-None kwargs, neither ``progress_callback`` nor
    ``read_timeout_seconds`` is added to the SDK call — preserves
    pre-#264 behaviour exactly so configs that omit the new keys see
    no observable change.
    """
    captured: dict[str, Any] = {}

    class _FakeResult:
        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _FakeSession:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            captured["kwargs"] = kwargs
            return _FakeResult()

    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    client._initialized = True
    client._session = _FakeSession()

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

    class _CapturingSession:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            # Capture the callback the op handler passed; the SDK would
            # invoke this with (progress, total, message) when the server
            # sends notifications/progress.
            captured_callback["cb"] = kwargs.get("progress_callback")
            captured_callback["read_timeout_seconds"] = kwargs.get(
                "read_timeout_seconds",
            )
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
    ctx = OpContext(
        workspace=None,  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        mcp_servers={"demo": {"type": "stdio", "command": "/bin/true"}},
    )
    # Pre-install a fake client so MCPClient construction is skipped.
    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    client._initialized = True
    client._session = _CapturingSession()
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})
    asyncio.run(mcp_op_handler._execute(op, ctx))

    # At least two mcp_progress events should have been emitted with the
    # structured fields the forwarder consumes.
    progress_events = [
        e.model_dump(mode="json") for e in events.all()
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
    ``timeout_seconds`` to ``MCPClient.call_tool`` (which converts to
    ``timedelta`` and passes as ``read_timeout_seconds`` to the SDK).
    """
    from reyn.core.op_runtime import mcp as mcp_op_handler

    captured: dict[str, Any] = {}

    class _FakeResult:
        def model_dump(self, mode: str = "json") -> dict:
            return {"content": [], "isError": False}

    class _CapturingSession:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _FakeResult:
            captured["read_timeout_seconds"] = kwargs.get(
                "read_timeout_seconds",
            )
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
    client._initialized = True
    client._session = _CapturingSession()
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})
    asyncio.run(mcp_op_handler._execute(op, ctx))

    read_to = captured["read_timeout_seconds"]
    assert isinstance(read_to, timedelta)
    assert read_to == timedelta(seconds=7.5)


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

        class _CapturingSession:
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
        client._initialized = True
        client._session = _CapturingSession()
        ctx.mcp_pool = _StubPool(client)

        op = MCPIROp(kind="mcp", server="demo", tool="thing", args={})
        asyncio.run(mcp_op_handler._execute(op, ctx))

        if expect_forwarded:
            assert "read_timeout_seconds" in captured["kwargs"], (
                f"cfg {cfg!r} should forward a FINITE default read_timeout_seconds (hung-server "
                f"guard), got kwargs={captured['kwargs']}"
            )
        else:
            assert "read_timeout_seconds" not in captured["kwargs"], (
                f"cfg {cfg!r} is an explicit opt-out (<=0) → NO read_timeout_seconds, "
                f"got kwargs={captured['kwargs']}"
            )


# ── 5. SDK-passing grep-pin (= belt-and-suspenders against future drift) ─


def test_mcp_client_call_tool_forwards_progress_and_timeout_kwargs() -> None:
    """Tier 2: source-level pin that ``MCPClient.call_tool`` constructs the
    SDK kwargs from ``progress_callback`` / ``timeout_seconds``. Catches
    a future refactor that quietly drops the kwargs without noticing
    the round-trip tests still pass (= belt-and-suspenders for the
    behavioural test above).
    """
    src = inspect.getsource(MCPClient.call_tool)
    # Both kwargs must appear in the SDK kwargs builder.
    assert "progress_callback" in src
    assert "read_timeout_seconds" in src
    assert "timedelta" in src, (
        "timeout_seconds must be converted to a timedelta before the SDK call"
    )
