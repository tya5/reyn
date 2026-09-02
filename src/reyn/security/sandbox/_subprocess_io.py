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

This module also owns ``kill_process_tree`` — the single shared cancel/timeout
reaper for every subprocess-launch site (the three sandbox backends + the CodeAct
runner). It kills the child TREE with a platform-correct mechanism: ``os.killpg``
on POSIX (whole process group) and ``taskkill /T`` on Windows (whole descendant
tree — ``terminate()``/``kill()`` alone reach only the direct process and orphan
grandchildren, #2292/#2715). One seam so the Windows gap is fixed once, not
re-fixed per site.
"""
from __future__ import annotations

import asyncio
import logging
import os
import select as _select
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

_logger = logging.getLogger(__name__)

# 10 MiB — single source for the per-stream subprocess-output ceiling. Distinct
# from the HTTP download ceiling (``reyn._http_limits.MAX_DOWNLOAD_BYTES``): this
# bounds child stdout/stderr capture, not a network body.
MAX_SUBPROCESS_OUTPUT_BYTES = 10 * 1024 * 1024

_READ_CHUNK = 32768
# Writing <= PIPE_BUF bytes to a ready pipe fd does not block (POSIX atomicity).
_PIPE_BUF = getattr(_select, "PIPE_BUF", 512)

# #4733 §3-a I/O ruling (architect, 2026-09-02): the ``fd_kind`` a ``sink``
# callback receives alongside each chunk — the POSIX fd numbers a child's
# stdout/stderr are conventionally attached to (1/2), reused here rather than
# inventing a new small enum for a 2-value distinction. If a caller directs
# both streams at ONE file, byte order between the two is NOT preserved
# (each stream is drained independently as it becomes ready) — a caller that
# cares about interleaving order must route stdout/stderr to separate sinks.
FD_STDOUT = 1
FD_STDERR = 2


def _guarded_sink(
    sink: "Callable[[int, bytes], None] | None",
) -> "Callable[[int, bytes], None] | None":
    """Wrap ``sink`` so ITS OWN exception cannot break the drain loop (#4733
    §3-a): a caller's sink is typically a file write (the async-exec runner's
    OWN file handle, per §3's MediaStore-has-no-append-API asymmetry) and a
    disk-full / permission failure there must not cost the exec its
    returncode or captured (capped) in-memory output. Drops the sink after
    its FIRST failure (not one retry per chunk — a failing sink is assumed to
    keep failing) and logs exactly ONE warning, not one per chunk."""
    if sink is None:
        return None
    state = {"failed": False}

    def _wrapped(fd_kind: int, data: bytes) -> None:
        if state["failed"]:
            return
        try:
            sink(fd_kind, data)
        except Exception:
            state["failed"] = True
            _logger.warning(
                "communicate_capped: sink raised — dropping it for the "
                "remainder of this drain (returncode/capped output are "
                "unaffected)", exc_info=True,
            )

    return _wrapped


def communicate_capped(
    proc: subprocess.Popen,
    *,
    input: bytes | None = None,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
    timeout: "float | None",
    sink: "Callable[[int, bytes], None] | None" = None,
) -> tuple[bytes, bytes, bool]:
    """Drain ``proc`` like ``proc.communicate(input, timeout)`` but cap stdout and
    stderr at ``max_bytes`` each (drain-and-discard the excess → bounded memory,
    preserved return code).

    ``sink`` (#4733 §3-a, architect ruling 2026-09-02): an optional
    ``(fd_kind, data) -> None`` callback invoked with EVERY chunk read from
    stdout (``FD_STDOUT``) or stderr (``FD_STDERR``), called BEFORE the cap
    check below — so a caller that wants the FULL stream (e.g. tee'd to a
    file for a still-running background exec to read a growing tail from,
    #4733's own async-exec) sees everything even past ``max_bytes``, while
    the in-memory ``(stdout, stderr, truncated)`` this function returns stays
    capped exactly as before. ``None`` (the default) is byte-identical to
    every one of this function's 18 existing call sites — nothing changes
    for a caller that doesn't pass it. A ``sink`` exception is caught and
    logged once, then dropped for the rest of this call (see
    :func:`_guarded_sink`) — this function's own contract (returncode +
    capped output) never depends on the sink succeeding.

    Returns ``(stdout, stderr, truncated)``. Raises
    :class:`subprocess.TimeoutExpired` (carrying the partial captured output) if
    ``timeout`` elapses before both streams close — parity with
    ``communicate(timeout=)``; the caller kills the process and handles it.

    #4271: ``timeout`` has NO default — every caller must decide. This was a
    ``None`` default (unbounded wait) until a real production defect surfaced:
    when a caller runs this via ``loop.run_in_executor`` (every async cancel-
    aware path does) and the child's pipe never reaches EOF (a killed process
    whose OWN child inherited the fd and stayed alive — #3862's exact bug
    shape, or any other case where the process being drained isn't the one
    actually holding the pipe open), the blocking read inside the executor's
    THREAD never returns. Because that thread is not a daemon thread, it
    blocks the whole interpreter from exiting — not just the awaiting
    coroutine, which DOES correctly give up at whatever `asyncio.wait_for`
    the caller wraps it in. A hang surfaces as a CI job timeout, not a test
    failure — the failure's own content is lost (`tests/environment/
    test_container_backend_cancel_3862.py`, discovered during #4264 ②'s
    falsify-verify). Removing the ``None`` default makes "wait forever"
    something every call site must write down, not something that happens
    by omission — the same class of hole stays closed for the NEXT caller,
    not just the ones fixed here. A caller for whom unbounded really is
    correct still can — write ``timeout=None`` explicitly.

    Platform dispatch: the POSIX selectors path uses ``select()``, which on Windows only polls
    SOCKETS, not pipe fds (``OSError [WinError 10038] not a socket``) — so on Windows we drain via
    a thread per stream instead (mirroring CPython's own Windows ``Popen._communicate`` branch).
    The two paths are contract-identical: same per-stream cap→``truncated``, same drain-and-discard
    past the cap (child never blocks on a full pipe), same ``TimeoutExpired`` (partial output).
    """
    if sys.platform == "win32":
        return _communicate_capped_threads(
            proc, input=input, max_bytes=max_bytes, timeout=timeout, sink=sink,
        )
    return _communicate_capped_selectors(
        proc, input=input, max_bytes=max_bytes, timeout=timeout, sink=sink,
    )


def _communicate_capped_threads(
    proc: subprocess.Popen,
    *,
    input: bytes | None,
    max_bytes: int,
    timeout: float | None,
    sink: "Callable[[int, bytes], None] | None" = None,
) -> tuple[bytes, bytes, bool]:
    """Windows drain: one daemon thread per stream (``select()`` can't poll pipe fds on Windows).

    Contract-identical to the selectors path: per-stream cap→``truncated``; drain-and-discard past
    the cap (the reader keeps consuming so the child never blocks on a full pipe = drain-safe); a
    still-running reader at the deadline → ``TimeoutExpired`` carrying the partial output. Mirrors
    CPython's Windows ``Popen._communicate`` (threads reading each stream to EOF), plus the cap.
    """
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    state = {"truncated": False}
    deadline = (time.monotonic() + timeout) if timeout is not None else None

    def _timed_out() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            proc.args, timeout, output=bytes(stdout_buf), stderr=bytes(stderr_buf)
        )

    _sink = _guarded_sink(sink)

    def _drain(stream, buf: bytearray, fd_kind: int) -> None:
        try:
            while True:
                data = stream.read1(_READ_CHUNK)
                if not data:  # EOF
                    break
                # #4733 §3-a: the sink sees every chunk, BEFORE the cap
                # check below — the file gets the full stream even past
                # max_bytes, the in-memory buf stays capped exactly as
                # before.
                if _sink is not None:
                    _sink(fd_kind, data)
                room = max_bytes - len(buf)
                if room > 0:
                    buf += data[:room]
                    if len(data) > room:
                        state["truncated"] = True
                else:
                    state["truncated"] = True  # at cap → drain-and-discard (keep reading)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _write_stdin(stream, data: bytes) -> None:
        try:
            stream.write(data)
        except (BrokenPipeError, OSError):
            pass  # child exited early — its return code is preserved
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threads: list[threading.Thread] = []
    if proc.stdout is not None:
        threads.append(
            threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, FD_STDOUT), daemon=True)
        )
    if proc.stderr is not None:
        threads.append(
            threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, FD_STDERR), daemon=True)
        )
    if proc.stdin is not None:
        if input:
            threads.append(
                threading.Thread(target=_write_stdin, args=(proc.stdin, input), daemon=True)
            )
        else:
            proc.stdin.close()
    for t in threads:
        t.start()

    for t in threads:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        t.join(remaining)
        if t.is_alive():  # still draining at the deadline → timeout (partial output captured)
            raise _timed_out()

    try:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        proc.wait(timeout=None if remaining is None else remaining)
    except subprocess.TimeoutExpired:
        raise _timed_out() from None
    return bytes(stdout_buf), bytes(stderr_buf), state["truncated"]


def _communicate_capped_selectors(
    proc: subprocess.Popen,
    *,
    input: bytes | None = None,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
    timeout: "float | None",
    sink: "Callable[[int, bytes], None] | None" = None,
) -> tuple[bytes, bytes, bool]:
    """POSIX drain via a selectors loop (3-way stdin/stdout/stderr concurrency), per-stream capped.
    The proven reference path (CPython POSIX ``Popen._communicate`` structure); see the public
    ``communicate_capped`` docstring for the contract."""
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    truncated = False
    _sink = _guarded_sink(sink)
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
                is_stdout = fobj is proc.stdout
                buf = stdout_buf if is_stdout else stderr_buf
                # #4733 §3-a: sink sees every chunk, BEFORE the cap check
                # below (see _communicate_capped_threads's own comment for
                # the shared rationale).
                if _sink is not None:
                    _sink(FD_STDOUT if is_stdout else FD_STDERR, data)
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


def _taskkill_tree(pid: int, *, force: bool) -> None:
    """Best-effort Windows process-TREE kill via ``taskkill /T`` (#2292).

    ``proc.terminate()`` / ``proc.kill()`` on Windows signal ONLY the direct
    process, so a sandboxed process that spawned grandchildren leaves ORPHANS on
    cancel/timeout. ``taskkill /T`` walks and terminates the whole descendant
    tree; ``/F`` forces it (the ``SIGKILL`` analog — without ``/F`` taskkill
    requests a graceful close first). Any failure (taskkill absent — e.g. this is
    a POSIX host under a no-``killpg`` test — or the pid already gone) is swallowed:
    this is a best-effort reaper on the cancel/timeout cleanup path.
    """
    cmd = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        cmd.insert(1, "/F")
    try:
        subprocess.run(  # noqa: S603 — fixed argv; pid is an int
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


async def kill_process_tree(proc: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    """Kill ``proc`` AND its child tree with a platform-correct mechanism —
    gracefully first, then forcefully after ``grace_seconds`` if still alive.

    - **POSIX**: ``os.killpg`` signals the whole process GROUP (SIGTERM → grace →
      SIGKILL). The spawn sites pass ``start_new_session=True`` so the child leads
      its own group and the group == the process tree.
    - **Windows** (no ``os.killpg`` / no process groups): ``taskkill /T`` walks and
      kills the descendant TREE. ``proc.terminate()`` / ``proc.kill()`` alone reach
      only the DIRECT process, leaving grandchildren orphaned (#2292) — so the
      graceful pass is ``taskkill /T`` + ``proc.terminate()`` and, after the grace,
      the forced pass is ``taskkill /F /T`` + ``proc.kill()``.

    Single shared reaper for every cancel/timeout cleanup site (the noop / seatbelt
    / landlock sandbox backends + the CodeAct runner) so the Windows tree-kill gap
    is fixed ONCE, not re-fixed per site (#2292, #2715). Requires a running event
    loop (blocking ``proc.wait`` / ``taskkill`` are offloaded to its executor).
    """
    loop = asyncio.get_running_loop()

    async def _wait_grace() -> bool:
        """True if the process exits within the grace window."""
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, proc.wait), timeout=grace_seconds,
            )
            return True
        except (asyncio.TimeoutError, Exception):
            return False

    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        if not await _wait_grace():
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    else:
        # Windows: kill the whole TREE (grandchildren included), not just the
        # direct process (#2292). Graceful tree request + terminate, then escalate
        # to a forced tree kill after the grace.
        await loop.run_in_executor(None, lambda: _taskkill_tree(proc.pid, force=False))
        try:
            proc.terminate()
        except OSError:
            pass
        if not await _wait_grace():
            await loop.run_in_executor(None, lambda: _taskkill_tree(proc.pid, force=True))
            try:
                proc.kill()
            except OSError:
                pass
