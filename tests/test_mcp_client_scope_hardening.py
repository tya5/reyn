"""Tier 2: MCP client-scope hardening — structural teardown + same-task fail-fast guard (#B).

Robust-by-construction replacement for the discipline-dependent ``teardown_mcp_clients()`` finally
line:
- ``ControlIRExecutor.mcp_client_scope()`` records the run-owning task on enter and closes every
  cached client in THAT task on exit — success, exception, or cancellation (single close site).
- ``op_runtime/mcp.py`` fails FAST if an MCP op runs in a task other than the scope owner (a
  cross-task client open would leak an anyio cancel-scope → the "cancel scope crossed task boundary"
  crash — the whole point of the same-task discipline). The unscoped chat per-call path
  (mcp_owner_task=None) is exempt.
Subprocess reuse (the cache) is preserved. Real executor + real ``_execute`` + injected fake client.
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.mcp.client as mcp_client_mod
from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl


class _FakeMCPClient:
    """Real (not mocked) stand-in recording close + the task it ran in."""

    instances: list = []

    def __init__(self, config, *, agent_id=None) -> None:
        self.closed = False
        self.close_task = None
        _FakeMCPClient.instances.append(self)

    async def initialize(self) -> None:
        pass

    async def list_tools(self):
        return [{"name": "t"}]

    async def call_tool(self, name, args, *, progress_callback=None, timeout_seconds=None):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False,
                "structuredContent": None}

    async def close(self) -> None:
        self.closed = True
        self.close_task = asyncio.current_task()


def _executor() -> ControlIRExecutor:
    events = EventLog()
    return ControlIRExecutor(workspace=Workspace(events=events), events=events)


# ── scope CM: records owner + structural teardown (success + exception) ──────────

@pytest.mark.asyncio
async def test_scope_records_owner_and_closes_on_exit():
    """Tier 2: the scope records the owning task on enter and closes every cached client in that
    task on exit, clearing the cache + resetting the owner."""
    ex = _executor()
    assert ex.mcp_scope_owner_task is None
    fake = _FakeMCPClient({})
    async with ex.mcp_client_scope():
        assert ex.mcp_scope_owner_task is asyncio.current_task()
        ex._mcp_clients["srv"] = fake  # simulate a client opened during the run

    assert fake.closed is True, "scope exit closes cached clients"
    assert fake.close_task is asyncio.current_task(), "closed in the OWNING task"
    assert ex.mcp_scope_owner_task is None, "owner reset after scope"


@pytest.mark.asyncio
async def test_scope_closes_on_exception():
    """Tier 2: the scope closes clients even when the body raises (structural teardown covers the
    error/cancellation path — the old finally line did too, but as a discipline-dependent call)."""
    ex = _executor()
    fake = _FakeMCPClient({})
    with pytest.raises(RuntimeError, match="boom"):
        async with ex.mcp_client_scope():
            ex._mcp_clients["srv"] = fake
            raise RuntimeError("boom")
    assert fake.closed is True, "scope closes clients on the exception path"
    assert ex.mcp_scope_owner_task is None


# ── fail-fast guard + reuse (op handler) ─────────────────────────────────────────

def _ctx(*, owner_task) -> OpContext:
    events = EventLog()
    return OpContext(
        workspace=Workspace(events=events), events=events,
        permission_decl=PermissionDecl(), permission_resolver=None,
        mcp_servers={"srv": {"type": "stdio", "command": "x"}},
        mcp_clients={}, mcp_owner_task=owner_task,
    )


@pytest.mark.asyncio
async def test_op_fails_fast_in_non_owner_task(monkeypatch):
    """Tier 2: CORE — an MCP op running in a task OTHER than the scope owner raises immediately
    (loud fail), instead of leaking a cross-task cancel-scope that crashes at teardown."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp
    monkeypatch.setattr(mcp_client_mod, "MCPClient", _FakeMCPClient)

    async def _noop():
        await asyncio.sleep(0)
    other_task = asyncio.create_task(_noop())  # a DIFFERENT task = the scope owner
    ctx = _ctx(owner_task=other_task)
    op = MCPIROp(kind="mcp", server="srv", tool="t", args={})
    with pytest.raises(RuntimeError, match="task other than the client-scope owner"):
        await _execute(op, ctx)
    await other_task


@pytest.mark.asyncio
async def test_op_proceeds_in_owner_task_and_reuses_client(monkeypatch):
    """Tier 2: in the owning task the op proceeds (byte-identical result), and a 2nd op to the same
    server REUSES the cached client — subprocess reuse preserved (no re-spawn), no N× spawn."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp
    monkeypatch.setattr(mcp_client_mod, "MCPClient", _FakeMCPClient)
    _FakeMCPClient.instances = []

    ctx = _ctx(owner_task=asyncio.current_task())  # owner == this task → guard passes
    op = MCPIROp(kind="mcp", server="srv", tool="t", args={})

    result = await _execute(op, ctx)
    assert result["status"] == "ok", "op proceeds + result byte-identical"
    client1 = ctx.mcp_clients["srv"]  # the client cached after call 1
    await _execute(op, ctx)  # 2nd call, same server
    assert ctx.mcp_clients["srv"] is client1, (
        "the SAME cached client is reused on the 2nd call — subprocess reuse preserved, not re-spawned"
    )


@pytest.mark.asyncio
async def test_unscoped_chat_path_is_exempt(monkeypatch):
    """Tier 2: the unscoped per-call path (mcp_owner_task=None, e.g. chat _mcp_call_tool) skips the
    guard — it opens+closes in its own task, so no owner is recorded."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp
    monkeypatch.setattr(mcp_client_mod, "MCPClient", _FakeMCPClient)

    ctx = _ctx(owner_task=None)  # unscoped
    op = MCPIROp(kind="mcp", server="srv", tool="t", args={})
    result = await _execute(op, ctx)
    assert result["status"] == "ok", "unscoped path proceeds (guard skipped)"
