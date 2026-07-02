"""Tier 2: _mcp_list_tools closes the MCP client on EVERY exit path + surfaces errors (list crash).

``Session._mcp_list_tools`` now routes through the ``MCPGateway`` seam (→ ``MCPClientPool`` →
``MCPClient``, #2421). The pool opens + closes the client in the SAME task on success, error, AND
cancellation, and the gateway contains any fault into an ``MCPFault`` → the list method returns an
``[{"error": …}]`` result instead of leaking the SDK's anyio cancel-scope cross-task (the owner-facing
``list_mcp_tools`` crash). This pins the close-on-error guarantee + same-task affinity + error
surfacing through the real Session, with the fake patched where the POOL constructs it. No subprocess.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import reyn.mcp.pool as pool_mod
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _session(tmp_path) -> Session:
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice")
    return reg.get_session("alice", sid)


class _FakeMCPClient:
    """A real (not mocked) stand-in for MCPClient that records close + the task it ran in.

    ``raises`` controls whether ``list_tools`` fails (the error path that the pre-fix code left
    unclosed)."""

    instances: list = []

    def __init__(self, config, *, agent_id=None) -> None:
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
    monkeypatch.setattr(sess, "_mcp_servers_flat", lambda: {"srv": {"command": "fake"}})


@pytest.mark.asyncio
async def test_list_tools_error_path_still_closes_same_task(tmp_path, monkeypatch):
    """Tier 2: CORE — when ``list_tools()`` RAISES, the client is STILL closed, in the SAME task.
    RED on the pre-fix code: close ran only after a successful list_tools() → skipped on error →
    leaked cancel-scope → cross-task crash."""
    sess = await _session(tmp_path)
    _install_fake(monkeypatch, sess, raises=True)

    result = await sess._mcp_list_tools("srv")

    assert any("boom from list_tools" in (e.get("error") or "") for e in result), \
        "the fault is surfaced as an error result (gateway describe_fault), not raised"
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

    result = await sess._mcp_list_tools("srv")

    assert result == [{"name": "some_tool"}]
    client = _FakeMCPClient.instances[-1]
    assert client.closed is True
    assert client.close_task is client.list_task
