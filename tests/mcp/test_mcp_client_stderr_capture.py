"""Tier 2: MCPClient stdio stderr capture for diagnostic readback.

When a self-made stdio MCP server exits immediately (e.g. import
error, missing dep, stdout pollution by a stray ``print``), the mcp
SDK surfaces the failure as ``"Connection close"`` with no way for
the user to know WHY the subprocess died. Pre-fix the subprocess
stderr went to ``sys.stderr`` of the parent (= the reyn TUI / chat
process), where it was often invisible. Post-fix, the client captures
stderr to a ``tempfile.TemporaryFile`` and includes the tail in the
``MCPError`` raised on init failure.

This file pins the contract independently of the mcp SDK:
  1. ``read_stderr_tail()`` returns captured text up to the configured
     byte cap; truncates with a ``...(truncated)`` prefix.
  2. ``close_stderr_capture()`` is idempotent and never raises.
  3. ``read_stderr_tail()`` on a missing / closed capture returns ``""``.
  4. The init-failure branch in ``_initialize_stdio()`` enriches MCPError
     with the captured tail when present — this ALSO proves the capture
     gets allocated and written to (#4282: the standalone allocation-only
     test that used to target ``_open_stdio()`` directly was removed as
     redundant with this one once ``_open_stdio`` itself was removed —
     see git history if you need the old form).

  5. #4285: ``test_initialize_failure_with_real_subprocess_captures_its_
     actual_stderr`` — a REAL child process's stderr actually reaches the
     ``errlog`` file the official SDK's ``stdio_client`` is given (the
     handoff item 4 above does not cover, since it fakes the CM instead of
     spinning a process; the SDK-level round-trip in
     ``tests/mcp/test_mcp_client.py`` never drives a failing server, so this
     gap was real and unwitnessed until now).
"""
from __future__ import annotations

import sys
import tempfile

import pytest

from reyn.mcp.client import MCPClient, MCPError


def _client(transport_type: str = "stdio") -> MCPClient:
    """Build a minimal MCPClient instance for state-level testing.

    Doesn't initialize — just constructs the object so the
    ``_open_stdio`` / capture helpers are reachable without a real
    transport.
    """
    if transport_type == "stdio":
        return MCPClient({"type": "stdio", "command": "/bin/true"})
    return MCPClient({"type": "streamable-http", "url": "http://localhost:9999/mcp"})


# ── 1. tail helpers handle absent capture gracefully ────────────────────


def test_read_stderr_tail_returns_empty_when_no_capture() -> None:
    """Tier 2: no capture configured → tail is empty string."""
    client = _client()
    assert client.stderr_capture is None
    assert client.read_stderr_tail() == ""


def test_close_stderr_capture_is_idempotent_with_no_capture() -> None:
    """Tier 2: closing a never-opened capture is a safe no-op."""
    client = _client()
    client.close_stderr_capture()  # must not raise
    client.close_stderr_capture()  # second call also safe


# ── 2. tail returns captured text ────────────────────────────────────────


def test_read_stderr_tail_returns_captured_content() -> None:
    """Tier 2: a tempfile with captured stderr text round-trips through tail."""
    client = _client()
    capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    capture.write("ImportError: No module named 'foo'\n")
    client._stderr_capture = capture
    tail = client.read_stderr_tail()
    assert "ImportError: No module named 'foo'" in tail


def test_read_stderr_tail_truncates_long_content() -> None:
    """Tier 2: content beyond the byte cap is truncated with a prefix.

    Prevents an MCPError message from ballooning when a server dumps
    a huge traceback before exit. The prefix tells the reader the
    output was cut.
    """
    client = _client()
    capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    long_text = "X" * (MCPClient.STDERR_TAIL_BYTES + 500)
    capture.write(long_text)
    client._stderr_capture = capture
    tail = client.read_stderr_tail()
    assert tail.startswith("...(truncated)")
    # Body length is capped at the configured byte limit.
    body = tail[len("...(truncated)\n"):]
    assert len(body) == MCPClient.STDERR_TAIL_BYTES


def test_close_stderr_capture_clears_attribute() -> None:
    """Tier 2: after close, ``_stderr_capture`` is None and tail is empty."""
    client = _client()
    client._stderr_capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    client.stderr_capture.write("anything")
    client.close_stderr_capture()
    assert client.stderr_capture is None
    assert client.read_stderr_tail() == ""


# ── 3. tail survives a closed underlying file (= defensive) ─────────────


def test_read_stderr_tail_returns_empty_when_file_closed() -> None:
    """Tier 2: a capture whose file was closed externally returns empty.

    Defensive: a future refactor might close the file before reading;
    the helper must not propagate the resulting ValueError as an
    MCPError contamination.
    """
    client = _client()
    capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    capture.write("should-not-leak")
    capture.close()
    client._stderr_capture = capture
    assert client.read_stderr_tail() == ""


# ── 4. http transport does not allocate a capture ────────────────────────


def test_http_transport_does_not_allocate_capture() -> None:
    """Tier 2: http transport leaves _stderr_capture as None.

    Capture is stdio-only; http transport has no subprocess. The
    field stays None so close() is a no-op.
    """
    client = _client("streamable-http")
    assert client.stderr_capture is None
    client.close_stderr_capture()  # safe


def test_initialize_failure_includes_stderr_tail_in_error(monkeypatch) -> None:
    """Tier 2: when initialize fails after stderr was written, the
    MCPError carries the tail as part of the message.

    #3698 stage 1: stdio now goes through ``_initialize_stdio`` directly
    (the official SDK's ``stdio_client``, not fastmcp's ``_open_transport``/
    ``_open_stdio`` — those are only reachable via the http/sse fastmcp path
    now). Patches ``mcp.client.stdio.stdio_client`` at the SOURCE module —
    ``_initialize_stdio``'s own ``from mcp.client.stdio import ...`` is a
    local import re-executed on every call, so it re-reads whatever this
    monkeypatch has installed there, same mechanism the module docstring's
    item 1 already relied on for the (now-superseded) fastmcp seam.

    ``_initialize_stdio`` allocates its OWN ``self._stderr_capture`` (it
    can't be pre-populated by the test the way the old fastmcp seam allowed
    — the real flow's own temp file didn't exist yet at that point either)
    — so the fake CM writes the diagnostic text into whatever file
    ``errlog=`` it's called with, simulating what a real subprocess's stderr
    capture would have produced before dying.
    """
    pytest.importorskip("mcp")
    client = _client()

    class _BrokenAsyncCM:
        def __init__(self, errlog):
            self._errlog = errlog

        async def __aenter__(self):
            if self._errlog is not None:
                self._errlog.write("Traceback: ImportError: missing dep 'foo'\n")
                self._errlog.flush()
            raise RuntimeError("subprocess died before handshake")

        async def __aexit__(self, *args):
            return False

    def _broken_stdio_client(params, errlog=None):
        return _BrokenAsyncCM(errlog)

    monkeypatch.setattr("mcp.client.stdio.stdio_client", _broken_stdio_client)

    import asyncio
    with pytest.raises(MCPError) as excinfo:
        asyncio.run(client.initialize())
    msg = str(excinfo.value)
    assert "MCP initialize failed" in msg
    assert "Traceback: ImportError: missing dep 'foo'" in msg
    # After error path, capture is closed.
    assert client.stderr_capture is None


def test_initialize_failure_with_real_subprocess_captures_its_actual_stderr() -> None:
    """Tier 2: #4285 — the subject is reyn's own boundary contract with the
    ``mcp`` SDK, NOT the SDK's own redirect implementation.

    Framed carefully because the naive framing ("does the SDK correctly
    redirect a child's stderr into errlog?") is a THIRD-PARTY property — the
    exact discriminator #3872 and #4291 both had to strip out of a test
    tonight. The question this test actually asks is reyn's own: **does the
    ``errlog`` reyn passes to ``stdio_client`` end up carrying the stderr of
    the child reyn told the SDK to start** — i.e. did #3698's fastmcp→SDK
    swap silently change the contract reyn depends on. If this assertion
    fails, that's reyn's problem either way: either reyn is no longer passing
    ``errlog`` correctly, or reyn is depending on an SDK contract that no
    longer holds — both are reyn's own integration boundary to notice, not
    the SDK's internals to verify.

    The test above (``test_initialize_failure_includes_stderr_tail_in_error``)
    proves the OTHER half of reyn's contract: whatever text lands in the
    ``errlog`` file gets surfaced into the ``MCPError`` message — but it
    drives that via a fake CM that writes into the file handle directly,
    never starting a real child process, so it never witnesses the handoff
    THIS test covers: a real subprocess's actual stderr output reaching that
    file in the first place. This spins a REAL child process that writes to
    stderr and exits nonzero — no fake CM, no wait-budget constant (the
    subprocess's own exit is the only thing waited on, via
    ``asyncio.run``'s normal await chain).
    """
    pytest.importorskip("mcp")
    client = MCPClient({
        "type": "stdio",
        "command": sys.executable,
        "args": [
            "-c",
            "import sys; sys.stderr.write('boom-from-a-real-child-process\\n'); sys.exit(1)",
        ],
    })

    import asyncio
    with pytest.raises(MCPError) as excinfo:
        asyncio.run(client.initialize())
    msg = str(excinfo.value)
    assert "boom-from-a-real-child-process" in msg, (
        f"the real child's actual stderr output did not reach the MCPError "
        f"message via the official SDK's errlog plumbing: {msg!r}"
    )
    assert client.stderr_capture is None
