"""Tests for the FastMCP-backed MCPClient (#2597 S1 — mcp SDK -> fastmcp swap).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock`` on
the transport or session. Stdio round-trips spawn a REAL subprocess running
``tests/_support/mcp_fastmcp_echo_server.py`` (a real FastMCP server, #4302:
now the official SDK's own bundled server framework — #4412 bumped reyn's
pin to ``mcp>=2.0,<3.0``, so this is ``mcp.server.mcpserver``'s
``MCPServer`` on the 2.0 line, not the 1.x ``mcp.server.fastmcp.FastMCP``
this echo server originally ported onto); http round-trips spin a REAL
local uvicorn server via ``FastMCP.run_streamable_http_async()`` on an
ephemeral port. Pagination is proven against a real low-level MCP server
(``tests/_support/mcp_paginated_tools_server.py``) that serves 2 pages.
"""
from __future__ import annotations

import asyncio
import socket
import sys

import pytest

from reyn.mcp.client import MCPClient, MCPError, expand_env
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"
_PAGINATED_SERVER = _SUPPORT_DIR / "mcp_paginated_tools_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _HttpEchoServer:
    """Runs the real echo FastMCP server in-process via ``run_streamable_http_async``
    on an ephemeral port, as a background asyncio task — no subprocess needed
    for the http-transport tests, but no mock either: a real bound socket
    serving the real MCP protocol."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp/"
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_HttpEchoServer":
        sys.path.insert(0, str(_SUPPORT_DIR))
        import mcp_fastmcp_echo_server as server_mod

        # #4412 pin-bump PR: mcp 2.0's `MCPServer.settings` DROPPED
        # `host`/`port` entirely (confirmed live: the `Settings` model no
        # longer declares those fields at all) — `run_streamable_http_async`
        # takes them as direct kwargs instead.
        # #4302: the bundled FastMCP/MCPServer lazily builds+CACHES a
        # StreamableHTTPSessionManager on first use, and its own docs say
        # "can only be called once per instance" — a 2nd http-transport test
        # importing the SAME cached module-level ``mcp`` object hangs on
        # startup otherwise (verified: isolated it to exactly this reuse).
        # Reset so each test gets a fresh session manager, matching this
        # module's fresh-``_HttpEchoServer``-per-test intent. On 2.0,
        # `MCPServer.session_manager` is a READ-ONLY property delegating to
        # the underlying `lowlevel.Server._session_manager` (confirmed live
        # by reading the property's own source) — that private attribute is
        # the actual resettable backing store.
        server_mod.mcp._lowlevel_server._session_manager = None
        self._task = asyncio.create_task(
            server_mod.mcp.run_streamable_http_async(host="127.0.0.1", port=self.port),
        )
        # Poll until the socket accepts connections instead of a fixed sleep.
        # Unbounded per the testing policy — a capped attempt count is a wait
        # duration rewritten as a count, and fails the same way on a slow host.
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort teardown
                pass


# ── round-trip tests ─────────────────────────────────────────────────────────


def test_stdio_transport_round_trip() -> None:
    """Tier 1: framework boundary — a real stdio subprocess handshakes, lists tools, and
    executes a tool call through the FastMCP-backed transport."""
    cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(_ECHO_SERVER)],
    }

    async def _run_it():
        async with MCPClient(cfg) as client:
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hello"})
            return tools, result

    tools, result = asyncio.run(_run_it())
    names = {t["name"] for t in tools}
    assert {"echo", "boom", "show_headers", "progress"} <= names
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "hello"


def test_http_transport_round_trip() -> None:
    """Tier 1: framework boundary — a real local HTTP MCP server (uvicorn via
    FastMCP.run_async) handshakes and executes a tool call over Streamable HTTP."""

    async def _run_it():
        async with _HttpEchoServer() as server:
            cfg = {
                "type": "streamable-http",
                "url": server.url,
                "headers": {"Authorization": "Bearer abc"},
            }
            async with MCPClient(cfg) as client:
                result = await client.call_tool("echo", {"text": "hi-http"})
                return result

    result = asyncio.run(_run_it())
    assert result["isError"] is False
    assert result["content"][0]["text"] == "hi-http"


def test_http_transport_forwards_agent_id_header() -> None:
    """Tier 1: FP-0016 Component E — ``X-Reyn-Agent-Id`` reaches the real server."""

    async def _run_it():
        async with _HttpEchoServer() as server:
            cfg = {"type": "streamable-http", "url": server.url}
            async with MCPClient(cfg, agent_id="reyn/test-agent") as client:
                result = await client.call_tool("show_headers", {})
                return result

    result = asyncio.run(_run_it())
    assert result["structuredContent"]["x-reyn-agent-id"] == "reyn/test-agent"


def test_list_tools_follows_pagination_cursor() -> None:
    """Tier 1: #2597 S1 free win — list_tools() follows nextCursor across pages instead
    of silently truncating at page 1 (the pre-swap bug). A real 2-page low-level server."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_PAGINATED_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            return await client.list_tools()

    tools = asyncio.run(_run_it())
    names = {t["name"] for t in tools}
    assert names == {"tool_0", "tool_1", "tool_2", "tool_3"}, (
        "all 4 tools across both pages must be returned, not just page 1's 2"
    )


def test_invalid_type_rejected() -> None:
    """Tier 1: MCPClient public contract — unsupported transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"type": "ftp", "url": "ftp://nope"})


def test_missing_type_rejected() -> None:
    """Tier 1: MCPClient public contract — missing transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"url": "http://x"})


def test_env_var_expansion(monkeypatch) -> None:
    """Tier 1: expand_env public contract — ${VAR} tokens in string values are replaced
    with the corresponding environment variable."""
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    monkeypatch.setenv("MY_HOST", "example.com")
    cfg = {
        "type": "streamable-http",
        "url": "https://${MY_HOST}/mcp",
        "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
    }
    expanded = expand_env(cfg)
    assert expanded["url"] == "https://example.com/mcp"
    assert expanded["headers"]["Authorization"] == "Bearer s3cret"


def test_env_var_expansion_stdio_env(monkeypatch) -> None:
    """Tier 1: framework boundary — expand_env in a stdio env dict propagates expanded
    values into the real subprocess's environment.

    #4282: the pre-#4282 form of this test inspected the constructed
    ``fastmcp.client.transports.StdioTransport`` object's ``.env`` directly
    (via the now-removed ``_open_transport``); that helper no longer
    exists — ``_initialize_stdio`` builds
    ``mcp.client.stdio.StdioServerParameters`` inline instead. The command
    here (a bare ``python -c ...`` printing text) is not a real MCP server,
    so driving this through a full ``initialize()`` would fail at the
    handshake for an unrelated reason; captures the REAL
    ``StdioServerParameters`` class's kwargs by wrapping it (still
    constructs the real object — the same seam
    ``test_mcp_client_stderr_capture.py``'s
    ``test_initialize_failure_includes_stderr_tail_in_error`` and
    ``test_mcp_client_sandbox_wrap.py``'s env test both already use) rather
    than faking the SDK type, and lets the (expected) handshake failure
    happen and get swallowed — the env is already captured before that
    point."""
    import mcp.client.stdio as stdio_mod

    monkeypatch.setenv("MY_TOKEN", "t0k")
    cfg = expand_env(
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-c",
                "import os,sys; sys.stdout.write(os.environ.get('API_TOKEN',''))",
            ],
            "env": {"API_TOKEN": "${MY_TOKEN}", **{"PATH": "/usr/bin:/bin"}},
        }
    )
    assert cfg["env"]["API_TOKEN"] == "t0k"

    captured: dict = {}
    real_params_cls = stdio_mod.StdioServerParameters

    def _capturing_params(*args, **kwargs):
        captured.update(kwargs)
        return real_params_cls(*args, **kwargs)

    monkeypatch.setattr(stdio_mod, "StdioServerParameters", _capturing_params)

    client = MCPClient(cfg)
    try:
        asyncio.run(client.initialize())
    except MCPError:
        pass  # expected — the command isn't a real MCP server
    finally:
        asyncio.run(client.close())

    assert captured.get("env", {}).get("API_TOKEN") == "t0k"


def test_close_releases_resources() -> None:
    """Tier 2: MCPClient lifecycle invariant — initialize sets is_initialized() True;
    close tears down the session (is_initialized() False) and is idempotent."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        assert client.is_initialized() is True
        await client.close()
        assert client.is_initialized() is False
        # Calling close again is a no-op (no exception raised).
        await client.close()
        assert client.is_initialized() is False

    asyncio.run(_run_it())


def test_initialize_against_unreachable_http_raises_mcp_error_not_cancelled() -> None:
    """Tier 1: #4282-adjacent falsify direction ① — a genuinely unreachable
    remote (connection refused, port 1) surfaces as MCPError, not a bare
    asyncio.CancelledError leaking past every except clause. #4283's CI red
    was exactly this: the official SDK's streamablehttp_client internally
    fails a POST-request task inside its OWN anyio task group, which cancels
    the sibling task our initialize() is awaiting on — asyncio.wait_for (the
    pre-fix code) corrupted anyio's per-task cancel-scope bookkeeping across
    that boundary, hiding the real ConnectError behind an uninformative
    CancelledError. See _close_stack_after_init_failure's docstring in
    client.py for the full root-cause and live-probe evidence."""
    cfg = {"type": "streamable-http", "url": "http://127.0.0.1:1/mcp", "timeout": 3}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()

    with pytest.raises(MCPError, match="MCP initialize failed"):
        asyncio.run(_run_it())


def test_initialize_cancelled_externally_propagates_as_cancellation_not_mcp_error(
    tmp_path,
) -> None:
    """Tier 1: #4282-adjacent falsify direction ② — a GENUINE external
    cancellation of the task running initialize() (mirrors
    reyn.core.cancellable.race_cancellable's watcher / Session's own
    hard-cancel calling Task.cancel()) must propagate as CancelledError, NOT
    get mistranslated into MCPError. Without this direction, a fix for ①
    that broadly caught every CancelledError and wrapped it in MCPError
    would make "the user pressed cancel" indistinguishable from "the server
    is unreachable" — exactly the trap lead-coder flagged on #4283's review.

    Uses the SAME real "silent stdio server" pattern (a subprocess that
    spawns and sleeps forever, never touching stdin/stdout) as
    ``test_3028_mcp_stdio_init_timeout.py`` — so initialize() is genuinely
    blocked waiting on I/O when the external cancel() lands, the same shape
    a Ctrl-C mid-handshake would hit in production. Exercises the stdio
    path specifically (lead-coder's review asked to check it alongside
    http/sse — its exception handling shares the same
    _close_stack_after_init_failure helper, so a discriminator bug here
    would misreport a stdio cancel too)."""
    server = tmp_path / "silent_server.py"
    server.write_text("import time\nwhile True:\n    time.sleep(3600)\n", encoding="utf-8")
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(server)]}

    async def _run_it():
        client = MCPClient(cfg)
        task = asyncio.ensure_future(client.initialize())
        # Give initialize() a moment to actually reach the blocked I/O wait
        # (not cancel it before it has even spawned the subprocess).
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_it())


def test_call_tool_propagates_tool_error_not_transport_crash() -> None:
    """Tier 1: framework boundary — a tool that raises server-side surfaces as
    ``isError: True`` in the result (MCP protocol-level tool error), not an MCPError —
    matching the pre-swap contract (``call_tool_mcp`` never raises on ``isError``)."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            return await client.call_tool("boom", {})

    result = asyncio.run(_run_it())
    assert result["isError"] is True
    # The text inside ``content`` is the SDK's own rendering of a server-side
    # tool exception, not reyn's: mcp 2.x changed it from the raised message
    # ("simulated tool failure") to a generic "Error executing tool <name>",
    # which turned every PR's CI red on an in-range bump (`mcp>=2.0,<3.0`).
    # What reyn owns here is the shape -- a tool error arrives as a RESULT
    # with isError set, never as a raised MCPError -- so that is what is
    # asserted. Asserting the string again re-pins a third party's wording.
    assert isinstance(result["content"][0]["text"], str)


def test_call_tool_propagates_transport_errors_as_mcp_error() -> None:
    """Tier 1: framework boundary — a genuine transport-level failure (the subprocess DIES
    mid-call, unlike ``boom``'s protocol-level ``isError`` result) is wrapped and surfaced
    as MCPError with a 'tools/call' message rather than a bare/uncontained exception."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            await client.call_tool("die", {})

    with pytest.raises(MCPError, match="tools/call"):
        asyncio.run(_run_it())


def test_sse_transport_round_trip() -> None:
    """Tier 1: #2597 S1 free win — SSE, previously an unconditional NotImplementedError,
    now round-trips against a real local SSE MCP server."""

    async def _run_it():
        port = _free_port()
        sys.path.insert(0, str(_SUPPORT_DIR))
        import mcp_fastmcp_echo_server as server_mod

        # #4412 pin-bump PR: `.settings.host`/`.settings.port` are gone on
        # mcp 2.0 — see `_HttpEchoServer.__aenter__` above for the same fix
        # and its full rationale.
        task = asyncio.create_task(
            server_mod.mcp.run_sse_async(host="127.0.0.1", port=port),
        )
        try:
            # Unbounded per the testing policy — see the identical poll in
            # _HttpEchoServer.__aenter__ above for the rationale.
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.05)
            cfg = {"type": "sse", "url": f"http://127.0.0.1:{port}/sse/"}
            async with MCPClient(cfg) as client:
                return await client.call_tool("echo", {"text": "sse-hi"})
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    result = asyncio.run(_run_it())
    assert result["isError"] is False
    assert result["content"][0]["text"] == "sse-hi"


# ── a359 P2: MCPClientPool same-task close-all + reuse ───────────────────────
# The pool replaces ControlIRExecutor.teardown_mcp_clients(): its __aexit__ closes every
# client opened via get() in the pool's (run-owning) task. Real MCPClient against real
# subprocesses; verified via the public is_initialized() surface.


def test_pool_closes_all_clients_on_scope_exit() -> None:
    """Tier 2: MCPClientPool.__aexit__ closes every client opened via get() in the pool's owning
    task — the a359 P2 replacement for teardown_mcp_clients (same-task close, robust-by-construction)."""
    from reyn.mcp.pool import MCPClientPool

    cfg_a = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
    cfg_b = {"type": "stdio", "command": sys.executable, "args": ["-c", "1"]}

    async def _run_it():
        pool = MCPClientPool()
        async with pool:
            client_a = await pool.get("a", cfg_a)
            assert client_a.is_initialized() is True
        assert client_a.is_initialized() is False, "client_a closed on scope exit"

    asyncio.run(_run_it())


def test_pool_reuses_cached_client_within_scope() -> None:
    """Tier 2: a 2nd get() for the same server reuses the cached client (subprocess reuse preserved,
    no re-spawn) — the whole reason the pool caches rather than opening per call."""
    from reyn.mcp.pool import MCPClientPool

    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClientPool() as pool:
            c1 = await pool.get("x", cfg)
            c2 = await pool.get("x", cfg)
            assert c1 is c2, "same cached client reused within the scope"

    asyncio.run(_run_it())
