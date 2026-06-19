"""Tier 2: sample_slack outbound tool — the in-process send tool (#1805).

A complete gateway plugin provides outbound (not just inbound). These pin the
``slack_send`` tool handler (incl. the crash-surface behaviour the feature
fixes: a failed send returns a surfaced error, not a silent drop), the
``register_tools`` contract, the ``load_webhook_tools`` collection through the
plugin entry point, and the ``build_server`` extra_tools dispatch.

A fake ``slack_sdk`` is injected so the success / crash-surface tests run in CI
(which installs ``[dev,mcp,web]`` but not the Slack SDK).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from reyn.gateway.sample_slack import register_tools
from reyn.gateway.sample_slack.webhook import build_send_tool


def _install_fake_slack(monkeypatch, *, raise_exc=None, captured=None):
    """Inject a fake ``slack_sdk.web.async_client.AsyncWebClient`` recording
    client so the handler runs without the real SDK."""
    class _FakeClient:
        def __init__(self, *, token):
            self.token = token

        async def chat_postMessage(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            if raise_exc is not None:
                raise raise_exc
            return {"ts": "1700000000.000100"}

    pkg = types.ModuleType("slack_sdk")
    web = types.ModuleType("slack_sdk.web")
    ac = types.ModuleType("slack_sdk.web.async_client")
    ac.AsyncWebClient = _FakeClient
    monkeypatch.setitem(sys.modules, "slack_sdk", pkg)
    monkeypatch.setitem(sys.modules, "slack_sdk.web", web)
    monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", ac)


# ── handler: validation (no SDK needed) ─────────────────────────────────────

def test_slack_send_requires_channel(monkeypatch):
    """Tier 2: missing channel → surfaced error (no send attempted)."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    tool = build_send_tool()
    out = json.loads(asyncio.run(tool.handler({"text": "hi"})))
    assert out["ok"] is False
    assert "channel" in out["error"]


def test_slack_send_requires_token(monkeypatch):
    """Tier 2: missing SLACK_BOT_TOKEN → surfaced error."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    tool = build_send_tool()
    out = json.loads(asyncio.run(tool.handler({"channel": "C1", "text": "hi"})))
    assert out["ok"] is False
    assert "SLACK_BOT_TOKEN" in out["error"]


# ── handler: send + crash-surface (faked SDK) ───────────────────────────────

def test_slack_send_success(monkeypatch):
    """Tier 2: a configured send posts to Slack and returns ok + ts."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    captured: dict = {}
    _install_fake_slack(monkeypatch, captured=captured)
    tool = build_send_tool()
    out = json.loads(asyncio.run(tool.handler(
        {"channel": "C1", "text": "hello", "thread_ts": "111.222"},
    )))
    assert out == {"ok": True, "ts": "1700000000.000100"}
    assert captured == {"channel": "C1", "text": "hello", "thread_ts": "111.222"}


def test_slack_send_surfaces_failure(monkeypatch):
    """Tier 2: CRASH-SURFACE — a failed Slack send returns a surfaced error,
    NOT a silent drop. This is the regression #1805 fixes (outbound failure was
    previously invisible because the separate MCP server failed in another
    process)."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    _install_fake_slack(monkeypatch, raise_exc=RuntimeError("slack 503"))
    tool = build_send_tool()
    out = json.loads(asyncio.run(tool.handler({"channel": "C1", "text": "hi"})))
    assert out["ok"] is False
    assert "slack 503" in out["error"]


# ── register_tools + load_webhook_tools collection ──────────────────────────

def test_register_tools_returns_slack_send():
    """Tier 2: register_tools (the outbound sibling of register_router) returns
    the slack_send tool."""
    tools = register_tools({"target_agent": "demo"})
    assert [t.name for t in tools] == ["slack_send"]


def test_load_webhook_tools_collects_via_entry_point():
    """Tier 2: load_webhook_tools resolves the plugin entry point and gathers
    its outbound tools (full discovery path: entry point → module →
    register_tools → tool)."""
    from reyn.interfaces.web.plugin_loader import load_webhook_tools

    tools = load_webhook_tools(
        webhooks_config={"sample_slack": {"target_agent": "demo"}},
    )
    assert "slack_send" in {t.name for t in tools}


def test_load_webhook_tools_skips_disabled():
    """Tier 2: a disabled plugin contributes no tools."""
    from reyn.interfaces.web.plugin_loader import load_webhook_tools

    tools = load_webhook_tools(
        webhooks_config={"sample_slack": {"target_agent": "d", "enabled": False}},
    )
    assert tools == []


# ── build_server extra_tools dispatch (in-process MCP server) ────────────────

def test_build_server_lists_and_dispatches_extra_tool(tmp_path):
    """Tier 2: build_server exposes an extra_tool in list_tools AND dispatches
    call_tool to its handler (the in-process MCP-server half of the e2e)."""
    pytest.importorskip("mcp")
    from mcp.types import (
        CallToolRequest,
        CallToolRequestParams,
        ListToolsRequest,
    )

    from reyn.core.events.state_log import StateLog
    from reyn.mcp.extra_tool import ExtraTool
    from reyn.mcp.server import build_server
    from reyn.runtime.registry import AgentRegistry

    async def _echo(args: dict) -> str:
        return json.dumps({"echo": args.get("msg", "")})

    tool = ExtraTool(
        name="echo_tool",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        },
        handler=_echo,
    )
    # The extra_tool dispatch never touches the registry, so a no-op
    # session_factory is enough to construct one.
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: None,
        state_log=state_log,
    )
    server = build_server(registry, extra_tools=[tool])

    list_handler = server.request_handlers[ListToolsRequest]
    names = {
        t.name
        for t in asyncio.run(
            list_handler(ListToolsRequest(method="tools/list", params=None)),
        ).root.tools
    }
    assert "echo_tool" in names
    assert {"list_agents", "send_to_agent", "answer_intervention"} <= names

    call_handler = server.request_handlers[CallToolRequest]
    result = asyncio.run(call_handler(CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="echo_tool", arguments={"msg": "hi"}),
    )))
    assert json.loads(result.root.content[0].text) == {"echo": "hi"}
