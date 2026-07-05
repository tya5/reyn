"""Real FastMCP server used as a test double for MCPClient round-trip tests (#2597 S1).

Run directly as a subprocess (stdio) or pointed at a host:port (http/sse) — never imported.
Tools:
  - ``echo(text)``       -> returns ``text`` verbatim.
  - ``boom()``           -> raises, so the server surfaces a tool-level error
                            (``isError: True``), never a transport crash.
  - ``show_headers()``   -> returns the incoming HTTP request headers (http/sse
                            transports only; used to prove header forwarding,
                            e.g. ``X-Reyn-Agent-Id``, reaches the real server).
  - ``progress(steps)``  -> reports ``steps`` progress notifications via the
                            real FastMCP ``Context.report_progress`` API, so
                            progress-callback plumbing is exercised against the
                            real protocol (not a hand-rolled fake).
  - ``notify_tool_list_changed()``   -> sends a real
                            ``notifications/tools/list_changed`` via
                            ``Context.send_notification`` (#2597 S2b — the
                            async notifications bridge).
  - ``notify_prompt_list_changed()`` -> sends a real
                            ``notifications/prompts/list_changed`` (#2597 S2b).
  - ``notify_log(level, logger_name, msg)`` -> sends a real
                            ``notifications/message`` (logging) via ``Context.log``
                            (#2597 S2b-log — the logging-notification consumption
                            slice of the async notifications bridge).
  - ``pid()``            -> returns ``os.getpid()`` of THIS server process. Used
                            by #2597 S2a connection-reuse tests to prove a second
                            ``call_tool`` hit the SAME held subprocess (no
                            re-handshake) rather than comparing Python object
                            identity alone.
  - ``bump()`` /         -> a per-process side-effect counter (#2597 S2a). ``bump``
    ``bump_then_die()``     increments + returns the count; ``bump_then_die``
                            increments THEN kills the subprocess AFTER the side
                            effect (drop-after-execution) — proves call_tool is
                            at-most-once across a mid-call drop (no double-count).

Usage:
  stdio: ``python mcp_fastmcp_echo_server.py``
  http:  ``python mcp_fastmcp_echo_server.py http <port>``
  sse:   ``python mcp_fastmcp_echo_server.py sse <port>``
"""
from __future__ import annotations

import sys

from fastmcp import Context, FastMCP

mcp = FastMCP("reyn-test-echo")


@mcp.tool()
def echo(text: str) -> str:
    return text


@mcp.tool()
def boom() -> str:
    raise RuntimeError("simulated tool failure")


@mcp.tool()
def die() -> str:
    """Kill the subprocess mid-call — simulates a genuine TRANSPORT failure (as opposed
    to ``boom``'s protocol-level tool error) so callers can distinguish MCPError
    (transport/connection broke) from a normal ``isError: True`` tool result."""
    import os

    os._exit(1)


@mcp.tool()
def pid() -> int:
    import os

    return os.getpid()


# #2597 S2a: a FILE-BACKED side-effect recorder. The count lives on disk (a byte
# appended per execution) so it SURVIVES the subprocess death — unlike an in-memory
# counter, which a fresh reconnected subprocess would reset. ``bump(path)`` records one
# execution; ``bump_then_die(path)`` records the side effect THEN kills the subprocess
# AFTER executing it (the drop-after-execution window). A caller that auto-retried
# ``bump_then_die`` would append TWICE (once per subprocess); at-most-once appends once.
@mcp.tool()
def bump(path: str) -> str:
    with open(path, "a", encoding="utf-8") as f:
        f.write("x")
    return "bumped"


@mcp.tool()
def bump_then_die(path: str) -> str:
    import os

    with open(path, "a", encoding="utf-8") as f:
        f.write("x")
        f.flush()
        os.fsync(f.fileno())
    # The side effect (the append) is durably on disk; now drop the transport BEFORE the
    # response reaches the client — the drop-after-execution window.
    os._exit(1)
    return "unreachable"


@mcp.tool()
def show_headers() -> dict:
    from fastmcp.server.dependencies import get_http_headers

    return dict(get_http_headers(include_all=True))


@mcp.tool()
async def progress(steps: int, ctx: Context) -> str:
    for i in range(1, steps + 1):
        await ctx.report_progress(progress=i, total=steps, message=f"step-{i}")
    return "done"


# #2597 S2b: real server-pushed list_changed notifications, for the async
# notifications-bridge tests (ReynMCPMessageHandler.on_tool_list_changed /
# on_prompt_list_changed). ``Context.send_notification`` sends immediately on the
# session — a real SEP-1686 notification, not a fake.
@mcp.tool()
async def notify_tool_list_changed(ctx: Context) -> str:
    import mcp.types as types

    await ctx.send_notification(types.ToolListChangedNotification())
    return "sent"


@mcp.tool()
async def notify_prompt_list_changed(ctx: Context) -> str:
    import mcp.types as types

    await ctx.send_notification(types.PromptListChangedNotification())
    return "sent"


# #2597 S2b-log: a real server-pushed logging notification (``notifications/message``),
# for the logging-consumption bridge test (ReynMCPMessageHandler.on_logging_message).
# ``Context.log`` sends immediately on the session (real MCP logging notification, per
# the 2025-11-25 spec — not a fake).
@mcp.tool()
async def notify_log(level: str, logger_name: str, msg: str, ctx: Context) -> str:
    await ctx.log(msg, level=level, logger_name=logger_name)
    return "sent"


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        transport, port = sys.argv[1], int(sys.argv[2])
        mcp.run(transport=transport, host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
