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
  1. ``_open_stdio()`` creates a temp file and passes it as
     ``errlog`` to ``stdio_client`` (= verified via signature
     introspection + the stand-in in ``test_mcp_client.py``).
  2. ``read_stderr_tail()`` returns captured text up to the configured
     byte cap; truncates with a ``...(truncated)`` prefix.
  3. ``close_stderr_capture()`` is idempotent and never raises.
  4. ``read_stderr_tail()`` on a missing / closed capture returns ``""``.
  5. The init-failure branch in ``initialize()`` enriches MCPError
     with the captured tail when present.

End-to-end repro of a self-made server crash is out of scope (= would
require spinning a subprocess); the SDK-level integration is verified
by the existing ``tests/test_mcp_client.py`` round-trip.
"""
from __future__ import annotations

import tempfile

import pytest

from reyn.mcp_client import MCPClient, MCPError


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


# ── 5. open_stdio sets up the capture as a side effect ──────────────────


def test_open_stdio_allocates_stderr_capture() -> None:
    """Tier 2: calling _open_stdio sets _stderr_capture to a TemporaryFile.

    Doesn't actually exercise the mcp SDK; we just observe the
    side-effect on the client instance. The returned context manager
    isn't entered.
    """
    pytest.importorskip("mcp")  # requires the optional dep
    client = _client()
    assert client.stderr_capture is None
    cm = client._open_stdio()
    try:
        assert client.stderr_capture is not None
        # writable text file with utf-8
        client.stderr_capture.write("hello")
        client.stderr_capture.flush()
        assert client.read_stderr_tail() == "hello"
    finally:
        client.close_stderr_capture()
        # cm is an async generator; we never entered it so no awaitable cleanup needed
        del cm


def test_initialize_failure_includes_stderr_tail_in_error() -> None:
    """Tier 2: when initialize fails after stderr was written, the
    MCPError carries the tail as part of the message.

    Uses the public ``initialize`` API with a deliberately broken
    transport opener that raises after writing to the capture file —
    simulating a subprocess that emits diagnostic text then exits.
    """
    pytest.importorskip("mcp")
    client = _client()
    # Pre-allocate a capture so the simulated transport can write into
    # it before the failure. Real flow allocates this in _open_stdio.
    client._stderr_capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    client._stderr_capture.write("Traceback: ImportError: missing dep 'foo'\n")
    client._stderr_capture.flush()

    class _BrokenAsyncCM:
        async def __aenter__(self):
            raise RuntimeError("subprocess died before handshake")

        async def __aexit__(self, *args):
            return False

    def _broken_transport():
        return _BrokenAsyncCM()

    client._open_transport = _broken_transport  # type: ignore[method-assign]

    import asyncio
    with pytest.raises(MCPError) as excinfo:
        asyncio.run(client.initialize())
    msg = str(excinfo.value)
    assert "MCP initialize failed" in msg
    assert "Traceback: ImportError: missing dep 'foo'" in msg
    # After error path, capture is closed.
    assert client.stderr_capture is None
