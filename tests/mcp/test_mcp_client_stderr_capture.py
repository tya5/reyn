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

End-to-end repro of a self-made server crash is out of scope (= would
require spinning a subprocess); the SDK-level integration is verified
by the existing ``tests/mcp/test_mcp_client.py`` round-trip.
"""
from __future__ import annotations

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
    return MCPClient({"type": "http", "url": "http://localhost:9999/mcp"})


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
    client._stderr_capture.write("anything")
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
    client = _client("http")
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
