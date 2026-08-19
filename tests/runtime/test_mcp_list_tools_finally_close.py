"""Tier 2: mcp_list_tools (EPHEMERAL session) closes the MCP client on EVERY exit path + surfaces
errors (list crash).

``RouterHostAdapter.mcp_list_tools`` (#3447: folded off ``Session._mcp_list_tools``, same seam,
same name, new home) routes an EPHEMERAL session's calls through the one-shot ``MCPGateway`` seam
(→ ``MCPClientPool`` → ``MCPClient``, #2421); #2597 S2a's held-open ``MCPConnectionService``
is the NON-ephemeral (persistent/main session) default instead — see
``test_2597_s2a_mcp_connection_service.py`` for that path's connection-reuse contract. The one-shot
pool opens + closes the client in the SAME task on success, error, AND cancellation, and the
gateway contains any fault into an ``MCPFault``. #3447 moved the ``Cancelled``/``MCPFault`` CATCH
off this seam and onto ``tools/mcp.py``'s ``_handle_list_mcp_tools`` (via ``_call_mcp_list``) — so
``RouterHostAdapter.mcp_list_tools`` itself now RAISES ``MCPFault`` on the error path instead of
returning an ``[{"error": …}]`` result (architect firm #3411: behavior-preserving at the LLM-visible
boundary, since the tool handler still reproduces that exact shape one layer up). This test pins the
close-on-error guarantee + same-task affinity at the ADAPTER seam, with the fake patched where the
POOL constructs it. No subprocess.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import reyn.mcp.pool as pool_mod
from reyn.core.events.state_log import StateLog
from reyn.mcp.gateway import MCPFault
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _session(tmp_path) -> Session:
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)
    sess = reg.get_session("alice", sid)
    # #2597 S2a: only an EPHEMERAL session still routes mcp_list_tools through the
    # one-shot MCPClientPool this test exercises — a persistent session now holds its
    # connection open via MCPConnectionService instead (see
    # test_2597_s2a_mcp_connection_service.py). #3447: the adapter's ephemeral_fn
    # reads this LIVE, so flipping it post-construction (as the registry does for a
    # real spawned session) is still observed correctly.
    sess._ephemeral = True
    return sess


class _FakeMCPClient:
    """A real (not mocked) stand-in for MCPClient that records close + the task it ran in.

    ``raises`` controls whether ``list_tools`` fails (the error path that the pre-fix code left
    unclosed)."""

    instances: list = []

    def __init__(self, config, *, agent_id=None, server_name=None) -> None:
        self.closed = False
        self.close_task = None
        self.list_task = None
        self._raises = getattr(_FakeMCPClient, "_next_raises", True)
        _FakeMCPClient.instances.append(self)

    async def __aenter__(self):
        # #a359 P1: mirror the real MCPClient's async-CM protocol (callers now use `async with`).
        return self

    async def __aexit__(self, *exc_info):
        await self.close()

    async def list_tools(self):
        self.list_task = asyncio.current_task()
        if self._raises:
            raise RuntimeError("boom from list_tools")
        return [{"name": "some_tool"}]

    async def close(self):
        self.closed = True
        self.close_task = asyncio.current_task()


def _install_fake(monkeypatch, sess, *, raises: bool) -> None:
    _FakeMCPClient.instances = []
    _FakeMCPClient._next_raises = raises
    monkeypatch.setattr(pool_mod, "MCPClient", _FakeMCPClient)  # where the pool constructs it
    # Supply one configured server so the method reaches the client lifecycle.
    # #3447: server-config resolution now lives on RouterHostAdapter itself
    # (its own _mcp_servers_flat, not Session's — the adapter builds the
    # gateway directly, no more session callback in between).
    monkeypatch.setattr(
        sess._router_host, "_mcp_servers_flat", lambda: {"srv": {"command": "fake"}},
    )


@pytest.mark.asyncio
async def test_list_tools_error_path_still_closes_same_task(tmp_path, monkeypatch):
    """Tier 2: CORE — when ``list_tools()`` RAISES, the client is STILL closed, in the SAME task.
    RED on the pre-fix code: close ran only after a successful list_tools() → skipped on error →
    leaked cancel-scope → cross-task crash."""
    sess = await _session(tmp_path)
    _install_fake(monkeypatch, sess, raises=True)

    with pytest.raises(MCPFault) as excinfo:
        await sess.router_host.mcp_list_tools("srv")
    assert "boom from list_tools" in str(excinfo.value), (
        "the fault propagates as MCPFault (#3447: the catch moved to tools/mcp.py's "
        "_handle_list_mcp_tools, not raised here)"
    )
    client = _FakeMCPClient.instances[-1]
    assert client.closed is True, "client MUST be closed on the error path (pool __aexit__)"
    assert client.close_task is client.list_task, (
        "close ran in the SAME task as list_tools — no cross-task cancel-scope boundary"
    )


@pytest.mark.asyncio
async def test_list_tools_success_path_closes_and_returns(tmp_path, monkeypatch):
    """Tier 2: the success path still returns the tools AND closes the client (no regression)."""
    sess = await _session(tmp_path)
    _install_fake(monkeypatch, sess, raises=False)

    result = await sess.router_host.mcp_list_tools("srv")

    assert result == [{"name": "some_tool"}]
    client = _FakeMCPClient.instances[-1]
    assert client.closed is True
    assert client.close_task is client.list_task
