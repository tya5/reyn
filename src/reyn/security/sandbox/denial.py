"""Pure classification of a sandbox denial from a finished result (#2820, part B;
network class added #5244 ①).

A sandbox that enforces ``(deny process-fork)`` (macOS seatbelt / Linux seccomp,
whenever ``SandboxPolicy.deny_subprocess`` is True) makes a *bare-command* exec
fail at the LAUNCHER layer rather than in the workload: a bare ``python3`` on
PATH resolves to a version-manager shim (``~/.pyenv/shims/python3`` → ``pyenv
exec ...``) or a spawn-based launcher (``npx`` / ``uvx``) whose own internal
``fork()`` is blocked — even when the command itself never forks. The raw stderr
is opaque::

    /opt/homebrew/opt/pyenv/bin/pyenv: fork: Operation not permitted

#5244 ①: the SAME opacity for a DIFFERENT denied syscall — a hook subprocess
whose own ``network:`` knob is unset/false gets an EPERM on ``connect()``
instead. Real-machine incident (issue #5244): a ``mcp_resource_updated`` hook
running an asyncio MCP client raised ``ExceptionGroup: unhandled errors in a
TaskGroup`` with no sandbox context anywhere — an operator had to work out from
first principles that ``network: true`` was the missing declaration. Captured
directly on this machine (macOS seatbelt, #5244 investigation) — a raw
``socket.connect()`` denial::

    PermissionError: [Errno 1] Operation not permitted

and the SAME underlying error, still present verbatim inside an asyncio
``TaskGroup``'s own exception aggregation (the actual #5244 shape)::

    ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
    ...
      PermissionError: [Errno 1] Operation not permitted

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

#: Denial class: the sandbox blocked an outbound ``connect()`` because the
#: caller's ``network:`` knob was unset/false. See module docstring (#5244).
DENIAL_NETWORK = "network_denied"

# The launcher-fork denial signature. A shell-based shim (pyenv/asdf/mise) or a
# spawn-heavy launcher (npx/uvx) prints "<name>: fork: <reason>" when the sandbox
# blocks fork(): "Operation not permitted" is the macOS sandbox-exec /
# (deny process-fork) EPERM case; "Resource temporarily unavailable" (EAGAIN) is
# the variant some Linux seccomp/rlimit configurations surface.
_FORK_DENIED = re.compile(
    r"fork:\s*(operation not permitted|resource temporarily unavailable)",
    re.IGNORECASE,
)

# #5244 ①: Python's own OSError formatting includes a trailing `: '<path>'`
# ONLY when its `filename` argument is set (a FILE op — e.g. a write-deny
# under a sandboxed `open()`, captured directly for contrast: `PermissionError:
# [Errno 1] Operation not permitted: '/tmp/x'`). A `socket.connect()` EPERM
# never sets `filename` — its own str() ends right after "Operation not
# permitted", with no trailing colon — captured verbatim above. The negative
# lookahead is what keeps a write-deny from misclassifying as a network deny
# (both raise EPERM=1; only the trailing-path presence distinguishes them).
#
# Scope, disclosed rather than oversold: this recognizes Python's own OSError
# text specifically (reyn's own hook ecosystem is Python-heavy) — a hook
# written in a different runtime produces a different error shape entirely,
# not covered here. Extend with a newly CAPTURED signature when one surfaces
# (this module's own discipline) — never a guessed pattern for an
# unconfirmed runtime.
_NETWORK_DENIED = re.compile(
    r"permissionerror:\s*\[errno 1\]\s*operation not permitted(?!:)",
    re.IGNORECASE,
)


def classify_denial(returncode: int, stderr: bytes | str) -> str | None:
    """Return a denial-class string for a finished sandbox result, or ``None``.

    Only a genuine failure (nonzero ``returncode``) is classified — a normal
    exit is never a denial regardless of its output. Recognizes the
    launcher-fork denial (:data:`DENIAL_FORK`, #2820) and the network-connect
    denial (:data:`DENIAL_NETWORK`, #5244 ①). ``None`` means "not a
    recognized sandbox denial", so callers special-case only the real thing.
    """
    if returncode == 0:
        return None
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if _FORK_DENIED.search(stderr):
        return DENIAL_FORK
    if _NETWORK_DENIED.search(stderr):
        return DENIAL_NETWORK
    return None


# #5832: real-machine incident (owner, 2026-09-06) — a shell hook's write
# under `open()` was denied (`write_paths:` unset for that hook), and the
# generic "shell-hook %r exited %d (stderr: %s)" fallback carried NO sandbox
# context at all: just the child's raw `PermissionError: [Errno 1] Operation
# not permitted: '<path>'`. lead-coder traced this to the actual cause in 10
# steps; the owner, seeing only the raw traceback, could not take step 1.
#
# Deliberately NOT a new DENIAL_WRITE class alongside DENIAL_FORK/
# DENIAL_NETWORK above: those two classifiers each name a SPECIFIC syscall
# and a SPECIFIC knob that fixes it (fork -> `subprocess: true`, connect ->
# `network: true`) -- a confident, narrow claim earned by a signature that
# (per #5244's own module docstring above) does not also occur for an
# ordinary non-sandbox failure. A write denial has no such disjoint
# signature: `PermissionError: [Errno 1] Operation not permitted: '<path>'`
# is IDENTICAL whether the sandbox denied the write or the real filesystem
# did (a read-only mount, a missing parent directory, an actual permissions
# problem) -- "EPERM means the sandbox did it" would be the exact bandaid
# lead-coder's #5832 brief rejected. So this function does not classify a
# denial at all -- it only decides whether a failure is PERMISSION-SHAPED
# enough that disclosing what reyn granted is worth the sentence. The
# caller states a FACT ("reyn ran this with write_paths=X"), never a
# diagnosis ("the sandbox caused this") -- the fact is true regardless of
# the real cause, and is exactly as far as reyn can honestly go on its own.
_PERMISSION_FAILURE_MARKERS = (
    "eperm",
    "eacces",
    "operation not permitted",
    "permission denied",
)


def looks_permission_related(stderr: bytes | str) -> bool:
    """Whether *stderr* looks like an OS-level permission failure of some
    kind — NOT a denial classifier (contrast :func:`classify_denial` above).

    This only gates whether reyn's granted sandbox range is worth disclosing
    alongside a failure; it never claims the sandbox caused it. See the
    module comment directly above this function for why a write denial
    specifically cannot be classified the way fork/network denials are.
    """
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    lowered = stderr.lower()
    return any(marker in lowered for marker in _PERMISSION_FAILURE_MARKERS)
