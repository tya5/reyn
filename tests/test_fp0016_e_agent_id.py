"""Tier 2: FP-0016 Component E — agent_id propagation contract.

Covers:
- AgentConfig default + parser
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

from unittest.mock import patch

import pytest

from reyn.config import (
    AgentConfig,
    ReynConfig,
    _build_agent_config,
    _default_agent_id,
)
from reyn.events.events import EventLog

# ── 1. AgentConfig + parser ────────────────────────────────────────────────


def test_default_agent_id_uses_hostname() -> None:
    """Tier 2: _default_agent_id returns reyn/<hostname>."""
    import socket
    expected = f"reyn/{socket.gethostname()}"
    assert _default_agent_id() == expected


def test_agent_config_default_id_is_reyn_hostname() -> None:
    """Tier 2: AgentConfig() defaults to reyn/<hostname>."""
    cfg = AgentConfig()
    assert cfg.id.startswith("reyn/")
    assert cfg.id == _default_agent_id()


def test_reyn_config_carries_agent_default() -> None:
    """Tier 2: ReynConfig default-constructs with AgentConfig."""
    cfg = ReynConfig()
    assert isinstance(cfg.agent, AgentConfig)
    assert cfg.agent.id.startswith("reyn/")


def test_parser_none_returns_default() -> None:
    """Tier 2: missing agent: block → default agent_id."""
    cfg = _build_agent_config(None)
    assert cfg.id == _default_agent_id()


def test_parser_empty_dict_returns_default() -> None:
    """Tier 2: empty agent: dict → default."""
    cfg = _build_agent_config({})
    assert cfg.id == _default_agent_id()


def test_parser_explicit_id_flows_through() -> None:
    """Tier 2: explicit agent.id is preserved verbatim."""
    cfg = _build_agent_config({"id": "reyn/acme-corp/code-review-agent"})
    assert cfg.id == "reyn/acme-corp/code-review-agent"


def test_parser_empty_string_falls_back_to_default() -> None:
    """Tier 2: empty-string id normalises to default (no empty agent_id leaks)."""
    cfg = _build_agent_config({"id": ""})
    assert cfg.id == _default_agent_id()


def test_parser_rejects_non_dict() -> None:
    """Tier 2: non-mapping → ValueError."""
    with pytest.raises(ValueError, match="must be a mapping"):
        _build_agent_config("not a dict")


def test_parser_rejects_non_string_id() -> None:
    """Tier 2: agent.id with non-string → ValueError."""
    with pytest.raises(ValueError, match="agent.id must be a string"):
        _build_agent_config({"id": 42})


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


def test_mcp_client_injects_x_reyn_agent_id_header() -> None:
    """Tier 2: MCPClient(agent_id=...) adds X-Reyn-Agent-Id to HTTP headers."""
    from reyn.mcp_client import MCPClient

    captured: dict = {}

    def fake_streamablehttp_client(url, headers=None, timeout=None):  # noqa: ARG001
        captured["headers"] = headers
        return None

    client = MCPClient(
        {"type": "http", "url": "https://example.com/mcp"},
        agent_id="reyn/test-agent",
    )
    with patch("mcp.client.streamable_http.streamablehttp_client",
               new=fake_streamablehttp_client):
        client._open_http()
    assert captured["headers"].get("X-Reyn-Agent-Id") == "reyn/test-agent"


def test_mcp_client_no_agent_id_no_header() -> None:
    """Tier 2: agent_id=None → no X-Reyn-Agent-Id header (= backwards compat)."""
    from reyn.mcp_client import MCPClient

    captured: dict = {}

    def fake_streamablehttp_client(url, headers=None, timeout=None):  # noqa: ARG001
        captured["headers"] = headers
        return None

    client = MCPClient({"type": "http", "url": "https://example.com/mcp"})
    with patch("mcp.client.streamable_http.streamablehttp_client",
               new=fake_streamablehttp_client):
        client._open_http()
    assert "X-Reyn-Agent-Id" not in (captured["headers"] or {})


def test_mcp_client_operator_header_wins() -> None:
    """Tier 2: operator-set X-Reyn-Agent-Id in config wins over agent_id arg.

    Operators may need to spoof for tests or proxy in production; respect
    their explicit header.
    """
    from reyn.mcp_client import MCPClient

    captured: dict = {}

    def fake_streamablehttp_client(url, headers=None, timeout=None):  # noqa: ARG001
        captured["headers"] = headers
        return None

    client = MCPClient(
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Reyn-Agent-Id": "reyn/spoofed"},
        },
        agent_id="reyn/auto",
    )
    with patch("mcp.client.streamable_http.streamablehttp_client",
               new=fake_streamablehttp_client):
        client._open_http()
    assert captured["headers"]["X-Reyn-Agent-Id"] == "reyn/spoofed"


# ── 4. OpContext.agent_id field ────────────────────────────────────────────


def test_op_context_agent_id_default_is_none() -> None:
    """Tier 2: OpContext.agent_id default None (= no auto-inject)."""
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl
    from reyn.workspace.workspace import Workspace

    ws = Workspace(events=EventLog(), skill_name="t")
    ctx = OpContext(
        workspace=ws,
        events=EventLog(),
        permission_decl=PermissionDecl(),
    )
    assert ctx.agent_id is None


def test_op_context_agent_id_flows_through() -> None:
    """Tier 2: OpContext(agent_id=...) is preserved."""
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl
    from reyn.workspace.workspace import Workspace

    ws = Workspace(events=EventLog(), skill_name="t")
    ctx = OpContext(
        workspace=ws,
        events=EventLog(),
        permission_decl=PermissionDecl(),
        agent_id="reyn/test",
    )
    assert ctx.agent_id == "reyn/test"
