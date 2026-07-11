"""Pure classification of a sandbox denial from a finished result (#2820, part B).

A sandbox that enforces ``(deny process-fork)`` (macOS seatbelt / Linux seccomp,
whenever ``SandboxPolicy.allow_subprocess`` is False) makes a *bare-command* exec
fail at the LAUNCHER layer rather than in the workload: a bare ``python3`` on
PATH resolves to a version-manager shim (``~/.pyenv/shims/python3`` → ``pyenv
exec ...``) or a spawn-based launcher (``npx`` / ``uvx``) whose own internal
``fork()`` is blocked — even when the command itself never forks. The raw stderr
is opaque::

    /opt/homebrew/opt/pyenv/bin/pyenv: fork: Operation not permitted

Two failure modes follow from that opacity: a weak model reads it as "I cannot
execute tools" and entrenches a false self-narrative turn after turn, and an
operator cannot tell an environment/PATH problem from a genuine tool failure.
This module names the class so the canonical layer can say "environment/config
problem, not tool-availability" and the audit-event can record it.

Pure: no I/O, no process state — a function of ``(returncode, stderr)`` only, so
it replays deterministically over a captured fixture (testing.md static-replay).
"""
from __future__ import annotations

import re

#: Denial class: the sandbox blocked ``fork()`` and a PATH launcher/shim (not the
#: workload) hit it. See module docstring for the mechanism.
DENIAL_FORK = "fork_denied"

# The launcher-fork denial signature. A shell-based shim (pyenv/asdf/mise) or a
# spawn-heavy launcher (npx/uvx) prints "<name>: fork: <reason>" when the sandbox
# blocks fork(): "Operation not permitted" is the macOS sandbox-exec /
# (deny process-fork) EPERM case; "Resource temporarily unavailable" (EAGAIN) is
# the variant some Linux seccomp/rlimit configurations surface.
_FORK_DENIED = re.compile(
    r"fork:\s*(operation not permitted|resource temporarily unavailable)",
    re.IGNORECASE,
)


def classify_denial(returncode: int, stderr: bytes | str) -> str | None:
    """Return a denial-class string for a finished sandbox result, or ``None``.

    Only a genuine failure (nonzero ``returncode``) is classified — a normal
    exit is never a denial regardless of its output — and currently the only
    class is the launcher-fork denial (:data:`DENIAL_FORK`, #2820). ``None``
    means "not a recognized sandbox denial", so callers special-case only the
    real thing.
    """
    if returncode == 0:
        return None
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if _FORK_DENIED.search(stderr):
        return DENIAL_FORK
    return None
