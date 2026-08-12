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
  - ``pid()``            -> returns ``os.getpid()`` of THIS server process. Used
                            by #2597 S2a connection-reuse tests to prove a second
                            ``call_tool`` hit the SAME held subprocess (no
                            re-handshake) rather than comparing Python object
                            identity alone.
  - ``resource://pid``   -> a RESOURCE whose content is this server process's PID
                            (the resource analog of ``pid()``). Used by #2597 slice
                            ②a to prove a 2nd ``read_resource`` hit the SAME held
                            subprocess.
  - ``pid_prompt``       -> a PROMPT whose rendered message is this server
                            process's PID (the prompt analog of ``pid()``). Used
                            by #2597 slice ②c to prove a 2nd ``get_prompt`` hit
                            the SAME held subprocess.
  - ``bump()`` /         -> a per-process side-effect counter (#2597 S2a). ``bump``
    ``bump_then_die()``     increments + returns the count; ``bump_then_die``
                            increments THEN kills the subprocess AFTER the side
                            effect (drop-after-execution) — proves call_tool is
                            at-most-once across a mid-call drop (no double-count).

Usage:
  stdio: ``python mcp_fastmcp_echo_server.py``
  http:  ``python mcp_fastmcp_echo_server.py http <port>``
  sse:   ``python mcp_fastmcp_echo_server.py sse <port>``

#4302: ported from the standalone ``fastmcp`` package to the official ``mcp``
SDK's own bundled server framework (same decorator API). #4412 (arc #4368)
later bumped the pin itself to ``mcp>=2.0,<3.0``, renaming the module this
imports from ``mcp.server.fastmcp``/``FastMCP`` (1.x) to
``mcp.server.mcpserver``/``MCPServer`` (2.0, see the import below) — the
decorator API (``@mcp.tool()`` etc.) is unchanged across that rename. This
was the last real fastmcp-server dependent, and porting it off the
standalone ``fastmcp`` package was the actual precondition for dropping
``fastmcp`` from ``pyproject.toml``'s core dependencies. Two ergonomic gaps vs standalone
fastmcp, both closed below: ``fastmcp.server.dependencies.get_http_headers()``
has no bundled equivalent (``show_headers`` reads the raw transport
``Request`` off ``ctx.request_context.request`` instead), and bundled
``Context`` has no ``send_notification`` convenience (the notify tools go
through ``ctx.session.send_notification`` directly — the same primitive
standalone fastmcp's own convenience wraps).
"""
from __future__ import annotations

import sys

from mcp.server.mcpserver import Context, MCPServer

mcp = MCPServer("reyn-test-echo")


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


# #2597 slice ②a: a resource whose CONTENT is this server process's PID — the
# resource analog of the ``pid()`` tool. Held-connection-reuse tests read it twice
# and assert the same PID, proving the 2nd read hit the SAME held subprocess (no
# re-handshake) — the resource-path twin of the S2a ``pid()`` tool round-trip.
@mcp.resource("resource://pid")
def pid_resource() -> str:
    import os

    return str(os.getpid())


# #2597 slice ②c: a prompt whose RENDERED MESSAGE is this server process's PID —
# the prompt analog of the ``pid()`` tool / ``resource://pid`` resource.
# Held-connection-reuse tests get it twice and assert the same PID, proving the
# 2nd get hit the SAME held subprocess (no re-handshake).
@mcp.prompt()
def pid_prompt() -> str:
    import os

    return str(os.getpid())


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
def show_headers(ctx: Context) -> dict[str, str]:
    # #4302: ``fastmcp.server.dependencies.get_http_headers()`` has no
    # bundled equivalent — the raw transport ``Request`` (a Starlette
    # request for http/sse) is threaded through ``RequestContext.request``
    # instead, carrying the same header data directly. Also: unlike
    # standalone fastmcp, a bare ``-> dict`` return annotation does NOT
    # auto-generate a structured-output schema on the bundled SDK — a
    # parametrized ``dict[str, str]`` is required for ``structuredContent``
    # to populate (verified against mcp 1.29.0).
    request = ctx.request_context.request
    if request is None:
        return {}
    return dict(request.headers)


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

    # #4302: bundled Context has no ``send_notification`` convenience — go
    # through ``ctx.session`` (the underlying ServerSession) directly.
    await ctx.session.send_notification(types.ToolListChangedNotification())
    return "sent"


@mcp.tool()
async def notify_prompt_list_changed(ctx: Context) -> str:
    import mcp.types as types

    await ctx.session.send_notification(types.PromptListChangedNotification())
    return "sent"


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        cli_transport, port = sys.argv[1], int(sys.argv[2])
        # #4412 pin-bump PR: mcp 2.0's `MCPServer.settings` DROPPED
        # `host`/`port` entirely (confirmed live: the `Settings` model no
        # longer declares those fields) — `.run(transport=..., **kwargs)`
        # forwards them straight to `run_streamable_http_async`/
        # `run_sse_async` as kwargs instead. This file's own CLI
        # ("http"/"sse" + port) stays unchanged for every caller.
        mcp.run(
            transport="streamable-http" if cli_transport == "http" else cli_transport,
            host="127.0.0.1", port=port,
        )
    else:
        mcp.run(transport="stdio")
