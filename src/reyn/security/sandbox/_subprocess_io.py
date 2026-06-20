"""Bounded subprocess I/O — a memory-capped ``communicate()`` for sandbox backends.

#1934 part C: ``subprocess.run(capture_output=True)`` / ``proc.communicate()``
materialise the ENTIRE child stdout+stderr in memory, so a sandboxed process
emitting unbounded output can OOM the host before the wall-clock timeout fires.

``communicate_capped`` writes stdin and reads stdout+stderr CONCURRENTLY via a
selectors loop — the same 3-way concurrency ``communicate()`` uses to avoid
pipe-buffer deadlock — but buffers at most ``max_bytes`` per stream: once a stream
reaches the cap its further output is drained-and-discarded (the child never
blocks on a full pipe, so its return code is preserved) and ``truncated`` is set.

Mirrors CPython's POSIX ``Popen._communicate`` selectors structure (the proven
reference) plus the per-stream cap. Stdlib-only; the seatbelt / landlock / noop
backends call it from both the blocking and the cancel-aware (#1470) paths.
"""
from __future__ import annotations

import os
import select as _select
import selectors
import subprocess
import time

# 10 MiB — single source for the per-stream subprocess-output ceiling. Distinct
# from the HTTP download ceiling (``reyn._http_limits.MAX_DOWNLOAD_BYTES``): this
# bounds child stdout/stderr capture, not a network body.
MAX_SUBPROCESS_OUTPUT_BYTES = 10 * 1024 * 1024

_READ_CHUNK = 32768
# Writing <= PIPE_BUF bytes to a ready pipe fd does not block (POSIX atomicity).
_PIPE_BUF = getattr(_select, "PIPE_BUF", 512)


def communicate_capped(
    proc: subprocess.Popen,
    *,
    input: bytes | None = None,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
    timeout: float | None = None,
) -> tuple[bytes, bytes, bool]:
    """Drain ``proc`` like ``proc.communicate(input, timeout)`` but cap stdout and
    stderr at ``max_bytes`` each (drain-and-discard the excess → bounded memory,
    preserved return code).

    Returns ``(stdout, stderr, truncated)``. Raises
    :class:`subprocess.TimeoutExpired` (carrying the partial captured output) if
    ``timeout`` elapses before both streams close — parity with
    ``communicate(timeout=)``; the caller kills the process and handles it.
    """
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    truncated = False
    input_view = memoryview(input) if input else None
    input_off = 0
    deadline = (time.monotonic() + timeout) if timeout is not None else None

    def _timed_out() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            proc.args, timeout, output=bytes(stdout_buf), stderr=bytes(stderr_buf)
        )

    with selectors.DefaultSelector() as sel:
        if proc.stdin is not None:
            if input_view is not None:
                sel.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
        if proc.stdout is not None:
            sel.register(proc.stdout, selectors.EVENT_READ)
        if proc.stderr is not None:
            sel.register(proc.stderr, selectors.EVENT_READ)

        while sel.get_map():
            wait: float | None = None
            if deadline is not None:
                wait = deadline - time.monotonic()
                if wait < 0:
                    raise _timed_out()
            for key, _events in sel.select(wait):
                fobj = key.fileobj
                if fobj is proc.stdin:
                    chunk = input_view[input_off:input_off + _PIPE_BUF]
                    try:
                        input_off += os.write(key.fd, chunk)
                    except (BrokenPipeError, OSError):
                        sel.unregister(fobj)
                        fobj.close()
                    else:
                        if input_off >= len(input_view):
                            sel.unregister(fobj)
                            fobj.close()
                    continue
                # stdout / stderr readable
                data = os.read(key.fd, _READ_CHUNK)
                if not data:  # EOF
                    sel.unregister(fobj)
                    fobj.close()
                    continue
                buf = stdout_buf if fobj is proc.stdout else stderr_buf
                room = max_bytes - len(buf)
                if room > 0:
                    buf += data[:room]
                    if len(data) > room:
                        truncated = True
                else:
                    truncated = True  # at cap → drain-and-discard

    # All pipes closed; reap the child (bound by any remaining deadline).
    try:
        remaining = (deadline - time.monotonic()) if deadline is not None else None
        proc.wait(timeout=None if remaining is None else max(0.0, remaining))
    except subprocess.TimeoutExpired:
        raise _timed_out() from None
    return bytes(stdout_buf), bytes(stderr_buf), truncated
