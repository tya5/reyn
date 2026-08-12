"""Tier 2: FP-0016 Component E — agent_id propagation contract.

Covers:
- the top-level `agent_id:` scalar default + parser (#4174 T5: flattened
  from `agent: {id: ...}`, a single-field namespace, to a plain scalar —
  same disposition as T1's `python:` deletion)
- EventLog auto-injects agent_id into every emit
- EventLog caller-provided agent_id wins over the injected one (= delegation)
- MCPClient adds X-Reyn-Agent-Id to HTTP headers
- MCPClient respects an operator-set X-Reyn-Agent-Id (= no override)
- OpContext.agent_id field flows through

No mocks; uses real instances and inspects public state via the public
EventLog API and the constructed streamablehttp_client call kwargs (via
a Fake module attribute swap).
"""

from __future__ import annotations

import pytest

from reyn.config import (
    ReynConfig,
    _build_agent_id,
    _default_agent_id,
)
from reyn.core.events.events import EventLog

# ── 1. the `agent_id:` scalar + parser (#4174 T5) ──────────────────────────


def test_default_agent_id_uses_hostname() -> None:
    """Tier 2: _default_agent_id returns reyn/<hostname>."""
    import socket
    expected = f"reyn/{socket.gethostname()}"
    assert _default_agent_id() == expected


def test_reyn_config_carries_agent_id_default() -> None:
    """Tier 2: ReynConfig default-constructs agent_id as a plain string,
    not a namespace — #4174 T5 removed the `AgentConfig` wrapper."""
    cfg = ReynConfig()
    assert isinstance(cfg.agent_id, str)
    assert cfg.agent_id.startswith("reyn/")
    assert cfg.agent_id == _default_agent_id()


def test_parser_none_returns_default() -> None:
    """Tier 2: missing agent_id: key → default agent_id."""
    assert _build_agent_id(None) == _default_agent_id()


def test_parser_explicit_id_flows_through() -> None:
    """Tier 2: an explicit agent_id: value is preserved verbatim."""
    assert _build_agent_id("reyn/acme-corp/code-review-agent") == (
        "reyn/acme-corp/code-review-agent"
    )


def test_parser_empty_string_falls_back_to_default() -> None:
    """Tier 2: empty-string agent_id normalises to default (no empty
    agent_id leaks)."""
    assert _build_agent_id("") == _default_agent_id()


def test_parser_rejects_non_string() -> None:
    """Tier 2: a non-string agent_id: value → ValueError."""
    with pytest.raises(ValueError, match="agent_id must be a string"):
        _build_agent_id(42)


# ── 2. EventLog auto-injection ─────────────────────────────────────────────


def test_event_log_injects_agent_id() -> None:
    """Tier 2: EventLog with agent_id stamps every event payload."""
    log = EventLog(agent_id="reyn/test-agent")
    event = log.emit("test_event", foo="bar")
    assert event.data["agent_id"] == "reyn/test-agent"
    assert event.data["foo"] == "bar"


def test_event_log_no_agent_id_means_no_injection() -> None:
    """Tier 2: EventLog without agent_id leaves payload unchanged."""
    log = EventLog()
    event = log.emit("test_event", foo="bar")
    assert "agent_id" not in event.data
    assert event.data["foo"] == "bar"


def test_event_log_caller_agent_id_wins() -> None:
    """Tier 2: explicit agent_id in emit kwargs is preserved (= delegation)."""
    log = EventLog(agent_id="reyn/host-agent")
    event = log.emit("test_event", agent_id="reyn/origin-agent", foo="bar")
    # Caller wins so multi-agent delegation can stamp the origin identity.
    assert event.data["agent_id"] == "reyn/origin-agent"


def test_event_log_agent_id_property_readable() -> None:
    """Tier 2: agent_id is exposed as a public property for downstream pickup."""
    log = EventLog(agent_id="reyn/test")
    assert log.agent_id == "reyn/test"
    assert EventLog().agent_id is None


# ── 3. MCPClient X-Reyn-Agent-Id header ────────────────────────────────────


def _capture_http_headers(monkeypatch) -> dict:
    """#4282: the pre-#4282 form of the three tests below inspected the
    constructed ``StreamableHttpTransport`` object's ``.headers`` directly
    (via the now-removed ``_open_http``); ``_initialize_http_or_sse`` calls
    ``mcp.client.streamable_http.streamablehttp_client(url, headers=...)``
    inline instead. Captures the REAL function's kwargs by wrapping it
    (still calls through to the real one) rather than faking the SDK —
    same seam ``test_config_mcp_headers.py`` already established.

    #4412 pin-bump PR: ``streamablehttp_client`` is GONE on mcp 2.0 —
    headers now reach the SDK's own ``create_mcp_http_client(headers=...)``
    factory instead (``client.py``'s replacement for constructing the
    transport's HTTP client) — see
    ``test_config_mcp_headers.py::_capture_streamablehttp_client_kwargs``'s
    identical fix, full detail."""
    import mcp.client.streamable_http as sh_mod

    captured: dict = {}
    real_create_client = sh_mod.create_mcp_http_client

    def _capturing(headers=None, timeout=None, auth=None):
        captured["headers"] = headers
        return real_create_client(headers=headers, timeout=timeout, auth=auth)

    monkeypatch.setattr(sh_mod, "create_mcp_http_client", _capturing)
    return captured


def _initialize_and_swallow_connect_failure(client) -> None:
    import asyncio

    from reyn.mcp.client import MCPError

    try:
        asyncio.run(client.initialize())
    except MCPError:
        pass  # expected — example.com is not a real MCP server
    finally:
        asyncio.run(client.close())


def test_mcp_client_injects_x_reyn_agent_id_header(monkeypatch) -> None:
    """Tier 2: MCPClient(agent_id=...) adds X-Reyn-Agent-Id to HTTP headers."""
    from reyn.mcp.client import MCPClient

    captured = _capture_http_headers(monkeypatch)
    client = MCPClient(
        {"type": "http", "url": "https://example.com/mcp", "init_timeout": 1},
        agent_id="reyn/test-agent",
    )
    _initialize_and_swallow_connect_failure(client)
    assert captured["headers"].get("X-Reyn-Agent-Id") == "reyn/test-agent"


def test_mcp_client_no_agent_id_no_header(monkeypatch) -> None:
    """Tier 2: agent_id=None → no X-Reyn-Agent-Id header (= backwards compat)."""
    from reyn.mcp.client import MCPClient

    captured = _capture_http_headers(monkeypatch)
    client = MCPClient({"type": "http", "url": "https://example.com/mcp", "init_timeout": 1})
    _initialize_and_swallow_connect_failure(client)
    assert "X-Reyn-Agent-Id" not in captured["headers"]


def test_mcp_client_operator_header_wins(monkeypatch) -> None:
    """Tier 2: operator-set X-Reyn-Agent-Id in config wins over agent_id arg.

    Operators may need to spoof for tests or proxy in production; respect
    their explicit header.
    """
    from reyn.mcp.client import MCPClient

    captured = _capture_http_headers(monkeypatch)
    client = MCPClient(
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Reyn-Agent-Id": "reyn/spoofed"},
            "init_timeout": 1,
        },
        agent_id="reyn/auto",
    )
    _initialize_and_swallow_connect_failure(client)
    assert captured["headers"]["X-Reyn-Agent-Id"] == "reyn/spoofed"


# ── 4. OpContext.agent_id field ────────────────────────────────────────────


def test_op_context_agent_id_default_is_none() -> None:
    """Tier 2: OpContext.agent_id default None (= no auto-inject)."""
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionDecl

    ws = Workspace(events=EventLog(), actor="t")
    ctx = OpContext(
        workspace=ws,
        events=EventLog(),
        permission_decl=PermissionDecl(),
    )
    assert ctx.agent_id is None


def test_op_context_agent_id_flows_through() -> None:
    """Tier 2: OpContext(agent_id=...) is preserved."""
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionDecl

    ws = Workspace(events=EventLog(), actor="t")
    ctx = OpContext(
        workspace=ws,
        events=EventLog(),
        permission_decl=PermissionDecl(),
        agent_id="reyn/test",
    )
    assert ctx.agent_id == "reyn/test"
