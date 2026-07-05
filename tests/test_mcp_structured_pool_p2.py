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
from reyn.mcp.pool import MCPClientPool, describe_fault, is_real_control_flow


def test_call_timeout_default_finite_override_optout():
    """Tier 2: S3 (tui dogfood gap) — the per-call MCP timeout defaults FINITE so a hung/slow server
    can't block the router loop forever; a per-server ``call_timeout_seconds`` overrides; ``<= 0``
    opts out (None). When the timeout fires, the SDK raises a TimeoutError which the op fault
    boundary already contains into an error result (→ LLM). Malformed → fail-safe finite default.
    #2421: the resolver now lives in the MCPGateway seam ([4], one place for every MCP op)."""
    from reyn.mcp.gateway import _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, resolve_call_timeout

    assert resolve_call_timeout({}) == _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, "finite default (not None)"
    assert resolve_call_timeout({"call_timeout_seconds": 5}) == 5.0, "per-server override"
    assert resolve_call_timeout({"call_timeout_seconds": 0}) is None, "0 opts out"
    assert resolve_call_timeout({"call_timeout_seconds": -1}) is None, "<0 opts out"
    assert resolve_call_timeout({"call_timeout_seconds": "x"}) == _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, \
        "malformed → fail-safe finite default"


def test_describe_fault_aggregates_group_members():
    """Tier 2: describe_fault summarises a group's members (type+message) for the LLM — not empty,
    not a raw traceback (owner req: the LLM must see WHAT failed)."""
    grp = ExceptionGroup("teardown", [RuntimeError("malformed response"), ValueError("reset")])
    text = describe_fault(grp)
    assert "malformed response" in text and "reset" in text
    assert "RuntimeError" in text and "ValueError" in text


# ── is_real_control_flow (#2421 seam predicate: cancelling()-gated) ─────────────────────

@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_is_real_control_flow_ki_se_always_propagate(exc_cls):
    """Tier 2: #2421 — KeyboardInterrupt / SystemExit are always real control flow — bare and
    group-nested — regardless of the task's cancel state."""
    assert is_real_control_flow(exc_cls()) is True
    assert is_real_control_flow(BaseExceptionGroup("g", [ConnectionResetError("x"), exc_cls()])) is True


def test_is_real_control_flow_spurious_cancel_is_contained():
    """Tier 2: #2421 — outside a genuine cancellation (current_task().cancelling() == 0) a
    CancelledError — bare OR in a cancel-mixed group — is SPURIOUS (an SDK-internal fold), NOT real
    control flow → contained. This is the crash-class fix (the conservative predicate re-raised it)."""
    assert is_real_control_flow(asyncio.CancelledError()) is False
    grp = BaseExceptionGroup("teardown", [ConnectionResetError("dead"), asyncio.CancelledError()])
    assert is_real_control_flow(grp) is False


def test_is_real_control_flow_plain_and_group_faults_contained():
    """Tier 2: #2421 — ordinary transport faults (bare or grouped) are never control flow."""
    assert is_real_control_flow(RuntimeError("t")) is False
    assert is_real_control_flow(ExceptionGroup("g", [RuntimeError("a"), ValueError("b")])) is False


@pytest.mark.asyncio
async def test_is_real_control_flow_genuine_cancel_only():
    """Tier 2: #2421 — a GENUINE run cancellation (the task is actually cancelled →
    current_task().cancelling() > 0) IS real control flow and propagates through the seam boundary,
    while a spurious CancelledError (no task cancel) is contained. Proves cancellation-safety is
    preserved — real cancels are never swallowed."""
    async def boundary(op):
        try:
            return await op()
        except BaseException as exc:  # noqa: BLE001 — mirrors the gateway seam boundary
            if is_real_control_flow(exc):
                raise
            return "contained"

    # genuine: cancel the task while it awaits inside the boundary → must propagate
    async def run():
        return await boundary(lambda: asyncio.sleep(10))
    t = asyncio.ensure_future(run())
    await asyncio.sleep(0.02)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    # spurious: a synthetic CancelledError with no task cancellation → contained
    async def raises_spurious():
        raise asyncio.CancelledError()
    assert await boundary(raises_spurious) == "contained"


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
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None, server_name=None: raising)

    async with MCPClientPool() as pool:
        await pool.get("srv", {"type": "stdio", "command": "x"})
    # reached here → the teardown group was contained (no crash out of the scope)
    assert raising.closed is True


@pytest.mark.asyncio
async def test_pool_contains_spurious_cancel_in_teardown(monkeypatch):
    """Tier 2: #2421 — a cancel-mixed teardown group from a dead subprocess — while our OWN task is
    NOT being cancelled — is SPURIOUS (anyio folding a faulted sibling), so the pool CONTAINS it
    rather than propagating (propagating it is the crash the conservative predicate did not prevent).
    A genuine run cancellation still propagates because ``current_task().cancelling() > 0`` — see
    ``test_is_real_control_flow_genuine_cancel_only``."""
    raising = _RaisingCloseClient(
        BaseExceptionGroup("teardown", [ConnectionResetError("subprocess died"), asyncio.CancelledError()])
    )
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None, server_name=None: raising)

    async with MCPClientPool() as pool:  # no raise — the spurious cancel-mixed teardown is contained
        await pool.get("srv", {"type": "stdio", "command": "x"})
    assert raising.closed is True, "teardown ran and its spurious cancel-mixed fault was contained"


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
@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
async def test_op_propagates_control_flow(exc_cls):
    """Tier 2: the op handler (via the gateway seam) never contains genuine control flow —
    KeyboardInterrupt / SystemExit propagate (an interrupted / exiting run must keep unwinding), even
    though ANY ordinary MCP fault is contained into an error result. (A genuine run cancellation also
    propagates; a SPURIOUS CancelledError with cancelling()==0 is contained — next test.)"""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    with pytest.raises(exc_cls):
        await _execute(MCPIROp(kind="mcp", server="srv", tool="t", args={}), _ctx(_FaultPool(exc_cls())))


@pytest.mark.asyncio
async def test_op_contains_spurious_cancel():
    """Tier 2: a SPURIOUS CancelledError from call_tool — our task NOT cancelling — is an
    SDK-internal artifact (a dead subprocess folding its task group), NOT a run cancellation, so it is
    CONTAINED into an error result. RED before the is_real_control_flow refinement (the conservative
    predicate re-raised any CancelledError → the crash)."""
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    result = await _execute(
        MCPIROp(kind="mcp", server="srv", tool="t", args={}), _ctx(_FaultPool(asyncio.CancelledError())),
    )
    assert result["status"] == "error" and result["kind"] == "mcp", "spurious cancel contained, not raised"


@pytest.mark.asyncio
async def test_pool_reraises_keyboardinterrupt_in_teardown(monkeypatch):
    """Tier 2: the pool re-raises a KeyboardInterrupt-containing teardown group (control flow is
    never contained) — the required refinement beyond cancellation."""
    raising = _RaisingCloseClient(BaseExceptionGroup("teardown", [KeyboardInterrupt()]))
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None, server_name=None: raising)

    with pytest.raises(BaseException) as ei:
        async with MCPClientPool() as pool:
            await pool.get("srv", {"type": "stdio", "command": "x"})
    assert is_real_control_flow(ei.value), "KeyboardInterrupt propagates, not contained"
