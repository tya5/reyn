"""Managed reyn-web subprocess — guaranteed teardown, orphan-leak fix (#268).

A bare ``subprocess.Popen`` + ``try/finally`` leaks reyn web when the driver
process dies via a *signal* (terminal close = SIGHUP, session end = SIGTERM,
Ctrl-C = SIGINT) BEFORE the ``finally`` block runs — the leftover ``reyn web``
reparents to launchd (ppid=1) and lingers. (2026-05-31: 27 such orphans on
ports 8282-8687, ~143MB, from a week of dogfood_step_2 runs whose drivers were
signal-killed.)

This module:
- spawns reyn web in its OWN session (``start_new_session=True``) so it leads a
  process group we can kill wholesale — covering any children reyn web forks;
- registers ``atexit`` + SIGTERM/SIGINT/SIGHUP handlers that group-kill every
  managed server, so teardown happens on normal exit, unhandled exception, AND
  catchable signals.

Residual: SIGKILL (-9) of the driver is uncatchable, so a hard ``kill -9`` can
still orphan — unavoidable from the parent side. Every other common death path
is now covered.

Usage:
    from _reyn_web_proc import spawn_reyn_web, managed_reyn_web

    proc = spawn_reyn_web([reyn_bin, "web", "--port", "8099"], cwd=..., env=...)
    # ... or, scoped:
    with managed_reyn_web([reyn_bin, "web", "--port", "8099"]) as proc:
        ...
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from contextlib import contextmanager

_MANAGED: "list[subprocess.Popen]" = []
_HANDLERS_INSTALLED = False


def _terminate(proc: "subprocess.Popen") -> None:
    """Group-kill a managed reyn web: SIGTERM the group, then SIGKILL on timeout."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()  # fallback: direct child only
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _cleanup_all() -> None:
    for proc in list(_MANAGED):
        _terminate(proc)
    _MANAGED.clear()


def _install_handlers() -> None:
    """Register atexit + signal cleanup once (idempotent)."""
    global _HANDLERS_INSTALLED
    if _HANDLERS_INSTALLED:
        return
    atexit.register(_cleanup_all)

    def _handler(signum, _frame):
        _cleanup_all()
        # Restore default disposition + re-raise so the process exits with the
        # conventional 128+signum status (don't swallow the signal).
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # e.g. not in the main thread, or signal unsupported on platform.
            pass
    _HANDLERS_INSTALLED = True


def spawn_reyn_web(cmd, **popen_kwargs) -> "subprocess.Popen":
    """``subprocess.Popen`` a reyn web command with guaranteed teardown.

    Forces ``start_new_session=True`` (own process group) unless the caller
    overrides it, and registers the process for atexit/signal cleanup.
    """
    _install_handlers()
    popen_kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(cmd, **popen_kwargs)
    _MANAGED.append(proc)
    return proc


def stop_reyn_web(proc: "subprocess.Popen") -> None:
    """Explicitly tear down a managed server now (group-kill) + deregister it.

    For callers that stop the server mid-run rather than at process exit. Safe
    to call more than once and on an already-dead process.
    """
    _terminate(proc)
    if proc in _MANAGED:
        _MANAGED.remove(proc)


@contextmanager
def managed_reyn_web(cmd, **popen_kwargs):
    """Context manager: spawn reyn web, guarantee teardown on block exit."""
    proc = spawn_reyn_web(cmd, **popen_kwargs)
    try:
        yield proc
    finally:
        _terminate(proc)
        if proc in _MANAGED:
            _MANAGED.remove(proc)


def _selftest() -> int:
    """Smoke: spawn a long sleeper via the manager, confirm it's killed on exit."""
    with managed_reyn_web([sys.executable, "-c", "import time; time.sleep(120)"]) as p:
        alive = p.poll() is None
    killed = p.poll() is not None
    print(f"spawned_alive={alive} killed_after_context={killed}")
    return 0 if (alive and killed) else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
