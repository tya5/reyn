"""Process-identity helpers for safe-mode python steps (FP-0042 Phase 2.2).

Safe-mode python bans ``import os``, but a small subset of os surface is
needed by stdlib skills — specifically PID identity for advisory locks
(``index_docs.write_chunks_with_lock`` records its own PID in
``.reyn/index/<source>/.lock`` and checks holder liveness on next
acquire).

The two functions here are *ambient* in the same sense as ``time`` and
``random``: they observe process state without performing file I/O,
network access, or operator-controlled state mutation. They do not need
a permission gate.

Surface:

- :func:`getpid` — current process id (= the subprocess running the
  safe-mode step). Returns ``os.getpid()`` verbatim.
- :func:`pid_alive` — whether a given PID is currently running. Mirrors
  the standard ``os.kill(pid, 0)`` liveness probe with the same
  semantics (= signal 0 does nothing but raises ``ProcessLookupError`` /
  ``PermissionError`` when the PID is unreachable).

Out of scope: anything beyond identity + liveness. Process spawning,
signal sending, environment / argv reads, and file-descriptor mucking
are correctly gated to unsafe-mode.
"""
from __future__ import annotations

import os as _os


def getpid() -> int:
    """Return the current process id.

    In safe-mode this is the python subprocess running the step. Lock
    files that record this PID become reapable as stale once the
    subprocess exits, which is the correct semantics for advisory locks
    scoped to the step's lifetime.
    """
    return _os.getpid()


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is currently a running process.

    Uses the standard ``os.kill(pid, 0)`` probe: signal 0 performs error
    checking without delivering a signal. Returns False on
    :class:`ProcessLookupError` (PID doesn't exist) and on
    :class:`OSError` more broadly (= covers Windows / restricted-cap
    edge cases where the call itself fails). Returns True when the
    probe succeeds, and also when the call raises
    :class:`PermissionError` — that means the PID exists but is owned
    by a different user, which still counts as "alive" for stale-lock
    purposes.
    """
    if pid <= 0:
        return False
    try:
        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user. From the
        # stale-lock-detection perspective, that's "alive".
        return True
    except OSError:
        return False
