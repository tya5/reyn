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
  - ``pid()``            -> returns ``os.getpid()`` of THIS server process. Used
                            by #2597 S2a connection-reuse tests to prove a second
                            ``call_tool`` hit the SAME held subprocess (no
                            re-handshake) rather than comparing Python object
                            identity alone.

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


@mcp.tool()
def show_headers() -> dict:
    from fastmcp.server.dependencies import get_http_headers

    return dict(get_http_headers(include_all=True))


@mcp.tool()
async def progress(steps: int, ctx: Context) -> str:
    for i in range(1, steps + 1):
        await ctx.report_progress(progress=i, total=steps, message=f"step-{i}")
    return "done"


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        transport, port = sys.argv[1], int(sys.argv[2])
        mcp.run(transport=transport, host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
