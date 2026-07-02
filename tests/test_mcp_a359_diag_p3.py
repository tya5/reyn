"""Tier 2: a359 P3 — the TEMPORARY Windows-verification diagnostic emits correctly.

The owner's Windows run relies on the ``reyn.mcp.a359diag`` INFO lines to confirm each MCP client is
opened AND closed in the SAME task with ``outcome=ok`` (no BaseExceptionGroup). This pins that the
instrumentation actually emits those lines on a normal pool cycle, so owner gets a real signal (a
silently-broken diagnostic would give false confidence). REMOVED in the follow-up together with the
a359-DIAG block (see docs/dev/mcp-a359-windows-verification.md).

TODO(a359-cleanup): remove this test with the a359-DIAG block once owner confirms the Windows crash
is gone. ``grep -rn "a359-cleanup"`` finds every removal point.
"""
from __future__ import annotations

import logging

import pytest

import reyn.mcp.pool as pool_mod
from reyn.mcp.pool import MCPClientPool


class _OKClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


@pytest.mark.asyncio
async def test_a359_diag_emits_open_and_close_same_task(monkeypatch, caplog):
    """Tier 2: a normal pool cycle emits the a359-diag open + close lines, with the same open/close
    task and outcome=ok — the signal owner reads on Windows to confirm the fix."""
    monkeypatch.setattr(pool_mod, "MCPClient", lambda cfg, *, agent_id=None: _OKClient())
    with caplog.at_level(logging.INFO, logger="reyn.mcp.a359diag"):
        async with MCPClientPool() as pool:
            await pool.get("srv", {"type": "stdio", "command": "x"})

    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "reyn.mcp.a359diag")
    assert "opened MCP client server=srv" in text, "open diagnostic emitted"
    assert "closed MCP client server=srv" in text and "outcome=ok" in text, "close diagnostic emitted"
