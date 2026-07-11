"""Resolve a launcher-shim ``argv[0]`` to the real binary OUTSIDE the sandbox (#2820, part A).

This is the actual fix for the launcher-fork denial that part B (``denial.py``)
only names. Under ``(deny process-fork)`` a bare command (``python3``) resolves
on PATH to a version-manager *shim* (``~/.pyenv/shims/python3`` → ``pyenv exec
…``); the shim's own ``fork()`` is blocked inside the sandbox even though the
workload never forks, so the whole exec dies with ``fork: Operation not
permitted``.

The fix keeps the boundary intact and moves the *launch machinery* out of it:
resolve ``argv[0]`` to the real interpreter/binary in the TRUSTED PARENT (which
may fork freely) and hand the sandbox child a real absolute path that does not
need to fork. The sandbox still enforces ``(deny process-fork)`` on the workload
— a program that itself tries to spawn is still denied; only the *shim*, whose
sole job is to pick and exec the real binary, is lifted out.

Version-manager fidelity: a shim's target depends on the manager's own selection
(``.python-version`` / ``.tool-versions`` in the run's cwd), so the real binary
is obtained by asking the manager (``pyenv which python3`` &c.) **with that cwd**,
not by grabbing the next same-named file on PATH (which could be a different,
wrong version). Every failure path is fail-open: if anything about the
resolution is uncertain, the original ``argv[0]`` is returned unchanged and the
pre-existing behavior (now *explained* by part B) stands — resolution never
changes *what* runs except to strip a shim indirection.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# A resolved path under one of these directory segments is a version-manager shim
# whose launch machinery forks. Keyed to the manager so we can ask the right tool.
_SHIM_MANAGERS: tuple[tuple[str, str], ...] = (
    ("/.pyenv/", "pyenv"),
    ("/pyenv/", "pyenv"),
    ("/.asdf/", "asdf"),
    ("/asdf/", "asdf"),
    ("/mise/", "mise"),
    ("/.local/share/mise/", "mise"),
    ("/rbenv/", "rbenv"),
    ("/.rbenv/", "rbenv"),
)

_SHIM_MARKER = "/shims/"

# Managers whose ``<manager> which <prog>`` prints the real absolute binary path.
_MANAGER_WHICH_TIMEOUT = 10.0


def _shim_manager(path: str) -> str | None:
    """Return the version-manager name if *path* is one of its shims, else None."""
    if _SHIM_MARKER not in path:
        return None
    for marker, manager in _SHIM_MANAGERS:
        if marker in path:
            return manager
    # A ``/shims/`` path we can't attribute to a known manager — treat as a shim
    # with no resolver (caller fails open), rather than guessing wrong.
    return "?"


def resolve_real_executable(
    argv0: str,
    *,
    env_path: str | None = None,
    cwd: str | None = None,
) -> str:
    """Return an absolute path to run in place of *argv0*, stripping a version-
    manager shim indirection when present. Fail-open: returns the plain PATH
    resolution (or *argv0* unchanged) whenever real resolution is unavailable.

    *env_path* is the ``PATH`` the sandbox child will see (so resolution matches
    what the child would resolve); *cwd* is the child's working directory (so the
    manager selects the version it would select for that directory). Both default
    to the parent process's values.
    """
    found = shutil.which(argv0, path=env_path)
    if found is None:
        # Not on PATH — nothing to resolve; hand back the original so the backend
        # produces its normal "not found" error (unchanged behavior).
        return argv0

    manager = _shim_manager(found) or _shim_manager(os.path.realpath(found))
    if manager is None:
        # A real binary already — return its absolute path (no shim indirection).
        return found
    if manager == "?":
        # A shim we can't resolve via a known manager — fail open to the shim.
        return found

    manager_bin = shutil.which(manager, path=env_path)
    if manager_bin is None:
        return found

    prog = os.path.basename(argv0)
    run_env = dict(os.environ)
    if env_path is not None:
        run_env["PATH"] = env_path
    try:
        proc = subprocess.run(
            [manager_bin, "which", prog],
            capture_output=True,
            text=True,
            timeout=_MANAGER_WHICH_TIMEOUT,
            cwd=cwd,
            env=run_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return found

    if proc.returncode == 0:
        resolved = proc.stdout.strip()
        if resolved and os.path.isabs(resolved) and os.path.exists(resolved):
            return resolved
    return found
