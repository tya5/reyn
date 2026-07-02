"""Tier 2: #2421 acceptance matrix — the MCPGateway seam contains every fault shape (defeat-all-
hypotheses) and re-raises only genuine control flow.

Owner steer: the traceback couldn't be captured, so the seam must defeat ALL hypotheses at once. This
injects each fault SHAPE into a fake client's ``list_tools`` (patched where the pool constructs it) and
asserts the gateway's behavior — exception-structure-independent containment:

  a1  cancel-mixed bare BaseExceptionGroup  (dead subprocess, our task not cancelling) → CONTAINED
  a2  all-Exception ExceptionGroup                                                      → CONTAINED
  c   plain transport Exception (BrokenResource / ConnectionReset)                      → CONTAINED
  d   genuine control flow (KeyboardInterrupt / SystemExit)                             → RE-RAISED

(b off-task is defeated structurally — the SDK stdio_client task group joins its reader/writer in the
pool's task; a genuine run cancellation propagating is covered in test_mcp_structured_pool_p2.)
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.mcp.pool as pool_mod
from reyn.mcp.gateway import MCPFault, MCPGateway

_CFG = {"type": "stdio", "command": "x"}


class _FaultClient:
    """A fake MCPClient whose ``list_tools`` raises a preset fault — mirrors the pool's construction
    surface (one positional config, keyword agent_id, async-CM)."""

    _next_exc: BaseException = RuntimeError("unset")

    def __init__(self, config, *, agent_id=None) -> None:
        self._exc = _FaultClient._next_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def list_tools(self):
        raise self._exc


def _inject(monkeypatch, exc: BaseException) -> None:
    _FaultClient._next_exc = exc
    monkeypatch.setattr(pool_mod, "MCPClient", _FaultClient)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    BaseExceptionGroup("teardown", [ConnectionResetError("dead"), asyncio.CancelledError()]),  # a1
    ExceptionGroup("bad", [RuntimeError("malformed"), ValueError("reset")]),                    # a2
    ConnectionResetError("broken pipe"),                                                        # c
], ids=["a1_cancel_mixed_group", "a2_all_exception_group", "c_plain_transport"])
async def test_gateway_contains_non_control_flow_faults(exc, tmp_path, monkeypatch):
    """Tier 2: #2421 — a non-control-flow fault of ANY shape (cancel-mixed group / all-Exception
    group / plain transport) is contained as an MCPFault, never a bare BaseExceptionGroup. The caller
    (list/probe/op) then shapes an error result → reyn survives."""
    monkeypatch.chdir(tmp_path)
    _inject(monkeypatch, exc)
    with pytest.raises(MCPFault) as ei:
        await MCPGateway().list_tools("srv", _CFG)
    assert str(ei.value), "the fault content is summarised for the LLM (non-empty)"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit], ids=["d_keyboardinterrupt", "d_systemexit"])
async def test_gateway_reraises_genuine_control_flow(exc_cls, tmp_path, monkeypatch):
    """Tier 2: #2421 — genuine control flow (KeyboardInterrupt / SystemExit) propagates through the
    seam untouched (an interrupted / exiting process must keep unwinding), NOT contained as MCPFault."""
    monkeypatch.chdir(tmp_path)
    _inject(monkeypatch, exc_cls())
    with pytest.raises(exc_cls):
        await MCPGateway().list_tools("srv", _CFG)
