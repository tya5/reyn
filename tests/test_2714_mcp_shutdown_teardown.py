"""#2714 — held MCP stdio subprocesses must be closed on the NORMAL-exit path.

The bug: the main interactive session's held MCP connections (Option C, #2597 S2a)
were closed only from ``registry.remove_session`` (spawned-session drop) and
``archive_agent`` (DELETE) — never on ordinary ``reyn chat`` exit (REPL /quit +
Ctrl-C/EOF → ``registry.shutdown()``). So every normal exit orphaned the held stdio
subprocess (``python -m <server>`` / ``uvx``); Unix reaps orphans, Windows does not,
so they accumulate in Task Manager.

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``. Stdio
round-trips spawn a REAL subprocess running ``tests/_support/mcp_fastmcp_echo_server.py``
(a real FastMCP server); its ``pid`` tool returns the subprocess's own OS pid, so the
tests observe actual process termination via ``os.kill`` rather than object identity.
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}


@pytest.mark.asyncio
async def test_shutdown_closes_main_session_held_mcp_connections(tmp_path: Path):
    """Tier 2: ``registry.shutdown()`` (the REPL /quit + Ctrl-C/EOF normal-exit seam)
    closes the MAIN session's held MCP connections, so a held stdio subprocess is not
    orphaned on ordinary exit (#2714). RED on origin/main (shutdown omitted the MCP
    teardown — held_servers stayed ``["srv"]`` after shutdown). Real AgentRegistry +
    real MAIN (default-sid) session + real stdio echo server (no mocks)."""
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    def _factory(profile) -> Session:
        return Session(agent_name=profile.name, mcp_servers={"srv": _CFG})

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory)
    registry.create("owner")
    session = registry.get_or_load("owner")  # the MAIN (default-sid) session

    await session._mcp_call_tool("srv", "echo", {"text": "hi"})
    assert session.mcp_held_servers() == ["srv"]

    await registry.shutdown()
    assert session.mcp_held_servers() == [], (
        "registry.shutdown() must close the main session's held MCP connections "
        "on the normal-exit path (#2714)"
    )


@pytest.mark.asyncio
async def test_close_reaps_subprocess_even_when_graceful_close_raises():
    """Tier 2: ``MCPClient.close()``'s belt-and-suspenders reap terminates the stdio
    subprocess even when the graceful fastmcp/mcp teardown RAISES (#2714 #C — the
    swallowed Windows teardown-fault path). The fix must GUARANTEE the OS subprocess
    is terminated, not trust that a contained fault left the child dead. Real
    subprocess (echo server); the graceful close is forced to raise by swapping in a
    real fake fastmcp client (a plain object whose ``close`` raises — no mock.patch)."""

    class _RaisingClose:
        async def close(self) -> None:
            raise RuntimeError("simulated Windows teardown fault")

    client = MCPClient(_CFG, agent_id="reyn/test", server_name="echo")
    await client.__aenter__()
    real_fastmcp = client._client  # keep for cleanup — we orphan it below
    try:
        pid = int((await client.call_tool("pid", {}))["content"][0]["text"])
        os.kill(pid, 0)  # process alive before close (no exception)

        client._client = _RaisingClose()  # graceful close will now raise
        await client.close()  # must NOT propagate the fault AND must still reap

        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # the belt-and-suspenders reap terminated the child
    finally:
        with contextlib.suppress(Exception):
            await real_fastmcp.close()


@pytest.mark.asyncio
async def test_graceful_close_alone_terminates_subprocess():
    """Tier 2: the happy-path counterpart — a clean ``MCPClient.close()`` (no injected
    fault) still terminates the stdio subprocess (the graceful fastmcp/mcp teardown is
    the PRIMARY reaper; the #2714 belt-and-suspenders is a no-op here). Guards against
    the reap wiring accidentally breaking the normal teardown."""
    client = MCPClient(_CFG, agent_id="reyn/test", server_name="echo")
    await client.__aenter__()
    pid = int((await client.call_tool("pid", {}))["content"][0]["text"])
    os.kill(pid, 0)  # alive

    await client.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # terminated on a clean close
