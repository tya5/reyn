"""Tier 2: a359 P2 — structured MCPClientPool + fault-isolation boundary.

The pool opens + reuses clients in the run-owning task and closes them there on scope exit
(structural teardown). Fault isolation (owner req): teardown faults — including a
``BaseExceptionGroup`` from the SDK's internal task group — are CONTAINED so a broken subprocess
teardown can't crash the run; and the op handler turns ANY MCP call fault (bad response / transport
group / server death) into an error tool-result so MCP misbehavior never crashes the router loop.
Neither ever swallows cancellation. Real pool + real op handler; injected raising client (no mock).
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.mcp.pool as pool_mod
from reyn.mcp.pool import MCPClientPool, describe_fault, is_or_contains_control_flow


def test_call_timeout_default_finite_override_optout():
    """Tier 2: S3 (tui dogfood gap) — the per-call MCP timeout defaults FINITE so a hung/slow server
    can't block the router loop forever; a per-server ``call_timeout_seconds`` overrides; ``<= 0``
    opts out (None). When the timeout fires, the SDK raises a TimeoutError which the op fault
    boundary already contains into an error result (→ LLM). Malformed → fail-safe finite default."""
    from reyn.core.op_runtime.mcp import _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, _resolve_call_timeout

    assert _resolve_call_timeout({}) == _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, "finite default (not None)"
    assert _resolve_call_timeout({"call_timeout_seconds": 5}) == 5.0, "per-server override"
    assert _resolve_call_timeout({"call_timeout_seconds": 0}) is None, "0 opts out"
    assert _resolve_call_timeout({"call_timeout_seconds": -1}) is None, "<0 opts out"
    assert _resolve_call_timeout({"call_timeout_seconds": "x"}) == _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, \
        "malformed → fail-safe finite default"


def test_describe_fault_aggregates_group_members():
    """Tier 2: describe_fault summarises a group's members (type+message) for the LLM — not empty,
    not a raw traceback (owner req: the LLM must see WHAT failed)."""
    grp = ExceptionGroup("teardown", [RuntimeError("malformed response"), ValueError("reset")])
    text = describe_fault(grp)
    assert "malformed response" in text and "reset" in text
    assert "RuntimeError" in text and "ValueError" in text


# ── is_or_contains_control_flow (the cancellation-safe boundary predicate) ─────────────

def test_cancel_predicate_direct():
    """Tier 2: a bare CancelledError is a cancellation."""
    assert is_or_contains_control_flow(asyncio.CancelledError()) is True


def test_cancel_predicate_group_with_cancel():
    """Tier 2: a BaseExceptionGroup containing a CancelledError counts (must be re-raised)."""
    grp = BaseExceptionGroup("teardown", [asyncio.CancelledError(), RuntimeError("BrokenResource")])
    assert is_or_contains_control_flow(grp) is True


def test_cancel_predicate_group_without_cancel():
    """Tier 2: a group of ordinary faults is NOT a cancellation → containable."""
    grp = ExceptionGroup("teardown", [RuntimeError("BrokenResource"), ValueError("bad response")])
    assert is_or_contains_control_flow(grp) is False


def test_cancel_predicate_plain_exception():
    """Tier 2: a plain non-control-flow exception is containable."""
    assert is_or_contains_control_flow(RuntimeError("t")) is False


@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_control_flow_predicate_keyboardinterrupt_systemexit(exc_cls):
    """Tier 2: KeyboardInterrupt / SystemExit are control flow — bare AND group-nested (mixed with a
    transport error) — so a Ctrl-C / process-exit during an MCP call or teardown still shuts down."""
    assert is_or_contains_control_flow(exc_cls()) is True
    grp = BaseExceptionGroup("teardown", [RuntimeError("BrokenResource"), exc_cls()])
    assert is_or_contains_control_flow(grp) is True


# ── pool teardown fault-containment ──────────────────────────────────────────────

class _RaisingCloseClient:
    """A client whose close (``__aexit__``) raises — models the SDK teardown fault."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        raise self._exc


@pytest.mark.asyncio
async def test_pool_contains_teardown_exception_group(monkeypatch):
    """Tier 2: CORE fault-isolation — a client whose teardown raises a transport BaseExceptionGroup
    is CONTAINED by the pool's __aexit__ (the run survives), not propagated as an uncontained crash."""
    raising = _RaisingCloseClient(
        BaseExceptionGroup("teardown", [RuntimeError("BrokenResourceError"), RuntimeError("ConnectionReset")])
    )
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None: raising)

    async with MCPClientPool() as pool:
        await pool.get("srv", {"type": "stdio", "command": "x"})
    # reached here → the teardown group was contained (no crash out of the scope)
    assert raising.closed is True


@pytest.mark.asyncio
async def test_pool_reraises_cancellation_in_teardown(monkeypatch):
    """Tier 2: the pool NEVER swallows cancellation — a teardown group containing a CancelledError
    is re-raised so a cancelled run keeps unwinding."""
    raising = _RaisingCloseClient(BaseExceptionGroup("teardown", [asyncio.CancelledError()]))
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None: raising)

    with pytest.raises(BaseException) as ei:
        async with MCPClientPool() as pool:
            await pool.get("srv", {"type": "stdio", "command": "x"})
    assert is_or_contains_control_flow(ei.value), "cancellation propagates, not contained"


# ── op-handler fault isolation (bad response / transport group → error result) ────

class _FaultPool:
    """A pool whose get() returns a client whose call_tool raises ``exc`` — to exercise the op
    handler's fault boundary."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return None

    @property
    def owner_task(self):
        return None

    async def get(self, server, config, *, agent_id=None):
        exc = self._exc

        class _C:
            async def call_tool(self, name, args, *, progress_callback=None, timeout_seconds=None):
                raise exc
        return _C()


def _ctx(pool):
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionDecl
    events = EventLog()
    return OpContext(
        workspace=Workspace(events=events), events=events,
        permission_decl=PermissionDecl(), permission_resolver=None,
        mcp_servers={"srv": {"type": "stdio", "command": "x"}}, mcp_pool=pool,
    )


@pytest.mark.asyncio
async def test_op_contains_bad_response_exception_group():
    """Tier 2: a transport/bad-response BaseExceptionGroup from call_tool → a contained error
    tool-result (owner req: MCP misbehavior must not crash the loop). RED if the boundary only
    caught `except Exception` (a BaseExceptionGroup would escape uncontained)."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    grp = BaseExceptionGroup("bad", [RuntimeError("malformed response"), RuntimeError("reset")])
    result = await _execute(MCPIROp(kind="mcp", server="srv", tool="t", args={}), _ctx(_FaultPool(grp)))
    assert result["status"] == "error", "MCP fault surfaced as an error result, not a crash"
    assert result["kind"] == "mcp"
    # owner req: the fault CONTENT reaches the LLM (non-empty; group members aggregated)
    assert result["error"], "error content is non-empty (fed to the LLM)"
    assert "malformed response" in result["error"] and "reset" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_cls", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
async def test_op_propagates_control_flow(exc_cls):
    """Tier 2: the op handler never contains control flow — CancelledError / KeyboardInterrupt /
    SystemExit propagate (a cancelled / interrupted / exiting run must keep unwinding), even though
    ANY ordinary MCP fault is contained into an error result."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    with pytest.raises(exc_cls):
        await _execute(MCPIROp(kind="mcp", server="srv", tool="t", args={}), _ctx(_FaultPool(exc_cls())))


@pytest.mark.asyncio
async def test_pool_reraises_keyboardinterrupt_in_teardown(monkeypatch):
    """Tier 2: the pool re-raises a KeyboardInterrupt-containing teardown group (control flow is
    never contained) — the required refinement beyond cancellation."""
    raising = _RaisingCloseClient(BaseExceptionGroup("teardown", [KeyboardInterrupt()]))
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None: raising)

    with pytest.raises(BaseException) as ei:
        async with MCPClientPool() as pool:
            await pool.get("srv", {"type": "stdio", "command": "x"})
    assert is_or_contains_control_flow(ei.value), "KeyboardInterrupt propagates, not contained"
