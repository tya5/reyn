"""Live-stdio MCP repro harness (#B / a359) — SCRATCH investigation tool (not a committed test).

Spawns a REAL minimal stdio MCP server subprocess and drives reyn's actual ``MCPClient`` lifecycle,
to (1) reproduce the ``stdio_client`` internal-anyio-task-group crash the fake-client tests were
blind to, (2) split platform-independent vs Windows-only, and (3) empirically answer
cacheable-vs-per-call (does a cached client survive a 2nd call + close without the task-boundary
crash?). Owner crash keywords: BaseExceptionGroup / cancel scope crossed task boundary /
BrokenResourceError / ConnectionReset.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import textwrap
from pathlib import Path

_SERVER_SRC = textwrap.dedent(
    '''
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("repro")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    if __name__ == "__main__":
        mcp.run(transport="stdio")
    '''
)

_KEYWORDS = ("baseexceptiongroup", "cancel scope", "task boundary",
             "brokenresource", "connectionreset", "connection lost", "unhandled errors in a task group")


def _cfg(server_path: str) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [server_path]}


async def scenario_same_task(server_path: str):
    """#2401's model: open + list_tools + close in ONE task. If this crashes, the SDK-internal task
    group violates task-affinity even under reyn's same-task close (= #2401 insufficient)."""
    from reyn.mcp.client import MCPClient
    client = MCPClient(_cfg(server_path))
    try:
        return {"tools": len(await client.list_tools())}
    finally:
        await client.close()


async def scenario_cacheable(server_path: str):
    """Cacheable test: open + call the tool TWICE (reuse) + close, all in one task. If it survives,
    a cached client IS reusable → #2403's (c) cache+scope holds. If it crashes on the 2nd call or
    close, per-call (a) is required."""
    from reyn.mcp.client import MCPClient
    client = MCPClient(_cfg(server_path))
    try:
        await client.list_tools()
        r1 = await client.call_tool("echo", {"text": "a"})
        r2 = await client.call_tool("echo", {"text": "b"})  # 2nd call = reuse the cached client
        return {"call1": bool(r1), "call2": bool(r2)}
    finally:
        await client.close()


async def scenario_cross_task(server_path: str):
    """Baseline: open in this task, close in a DIFFERENT task → the known cross-task hazard."""
    from reyn.mcp.client import MCPClient
    client = MCPClient(_cfg(server_path))
    await client.list_tools()

    async def _close_elsewhere():
        await client.close()

    await asyncio.create_task(_close_elsewhere())
    return {"closed_in": "other_task"}


async def scenario_no_close_gc(server_path: str):
    """Harshest: open + list_tools, then RETURN WITHOUT close → the AsyncExitStack is finalised by
    GC / loop teardown from an unrelated context (the exact case the G11 comment warned about). Most
    likely to trip 'cancel scope crossed task boundary' if anything does."""
    from reyn.mcp.client import MCPClient
    client = MCPClient(_cfg(server_path))
    tools = await client.list_tools()
    return {"tools": len(tools), "closed": False}  # intentionally leak → GC teardown


async def scenario_real_session_list_tools(server_path: str):
    """The ACTUAL owner path: Session._mcp_list_tools (post-#2401 finally-close) against a real
    stdio subprocess."""
    import tempfile as _tf

    from reyn.core.events.state_log import StateLog
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    d = _tf.mkdtemp()
    sl = StateLog(Path(d) / "wal.jsonl")
    holder: dict = {}

    def _factory(profile):
        s = Session(agent_name=profile.name, state_log=sl, registry=holder.get("reg"))
        s.register_intervention_listener("t")
        return s

    reg = AgentRegistry(project_root=Path(d), session_factory=_factory, state_log=sl)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(Path(d) / ".reyn" / "agents" / "alice")
    sess = reg.get_or_load("alice")
    sess._mcp_servers_flat = lambda: {"srv": _cfg(server_path)}  # type: ignore
    tools = await sess._mcp_list_tools("srv")
    return {"tools": tools if isinstance(tools, list) and tools and "error" in tools[0] else len(tools)}


async def scenario_fault_server_death(_server_path: str):
    """Fault-injection (owner req): a server that DIES on startup. reyn must CONTAIN it (a catchable
    error the callers wrap → error result), not an uncontained crash. Structured `async with` path."""
    from reyn.mcp.client import MCPClient
    dead = {"type": "stdio", "command": sys.executable, "args": ["-c", "import sys; sys.exit(1)"]}
    try:
        async with MCPClient(dead) as client:
            await client.list_tools()
        return {"contained": False, "note": "unexpected: no error"}
    except Exception as exc:  # noqa: BLE001 — a CONTAINABLE error (callers wrap → error result)
        return {"contained": True, "error_type": type(exc).__name__}


def _run_isolated(name: str, make_coro, server_path: str) -> None:
    """Each scenario in its OWN fresh event loop (asyncio.run) so a leaked task/scope from one
    scenario can't contaminate the next — clean per-scenario verdict."""
    try:
        result = asyncio.run(make_coro(server_path))
        print(f"[{name}] SURVIVED: {result}")
    except BaseException as exc:  # noqa: BLE001 — investigation: catch everything incl. groups
        text = f"{type(exc).__name__}: {exc}"
        print(f"[{name}] CRASHED: {text}")
        hits = [k for k in _KEYWORDS if k in text.lower()]
        if hits:
            print(f"    → owner-keyword match: {hits}")


if __name__ == "__main__":
    loop_cls = type(asyncio.new_event_loop()).__name__
    print(f"python={sys.version.split()[0]} platform={sys.platform} loop={loop_cls}")
    with tempfile.TemporaryDirectory() as _d:
        _server = str(Path(_d) / "repro_server.py")
        Path(_server).write_text(_SERVER_SRC, encoding="utf-8")
        _run_isolated("same_task_open_close", scenario_same_task, _server)
        _run_isolated("cacheable_2calls_close", scenario_cacheable, _server)
        _run_isolated("cross_task_close", scenario_cross_task, _server)
        _run_isolated("no_close_gc_teardown", scenario_no_close_gc, _server)
        _run_isolated("real_session_list_tools", scenario_real_session_list_tools, _server)
        _run_isolated("fault_server_death", scenario_fault_server_death, _server)
