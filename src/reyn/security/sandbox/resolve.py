"""Resolve a launcher-shim ``argv[0]`` to the real binary — FILESYSTEM-ONLY (#2820, part A).

This is the fix for the launcher-fork denial that part B (``denial.py``) only
names. Under ``(deny process-fork)`` a bare command (``python3``) resolves on
PATH to a version-manager *shim* (``~/.pyenv/shims/python3`` → ``pyenv exec …``);
the shim's own ``fork()`` is blocked inside the sandbox even though the workload
never forks, so the whole exec dies with ``fork: Operation not permitted``.

The fix strips the shim indirection by reading the version-manager's ON-DISK
layout — **no subprocess, no exec**. A bare ``python3`` that resolves to a pyenv
shim is rewritten to ``$PYENV_ROOT/versions/<selected>/bin/python3``, a real
binary that runs directly under the sandbox without a launch fork. The
``(deny process-fork)`` boundary is unchanged — a program that itself tries to
spawn is still denied; only the shim's indirection is removed.

Why filesystem-only (not ``<manager> which``): invoking the manager would run it
as an UNSANDBOXED subprocess with the child's (agent/tool-writable) ``cwd`` and
the full parent env. Some managers evaluate their per-directory config as a
template that can ``exec()`` (e.g. ``mise`` / CVE-2026-33646), so an attacker who
writes a crafted config into the workspace could achieve RCE **outside** the
sandbox — strictly worse than the denial we set out to fix. Reading the layout
as plain data has no such surface: an attacker-writable ``.python-version`` is
consumed only as a version *token* (strictly validated), which at most selects a
different already-installed version's real binary (still sandboxed — no
escalation). Managers whose selection cannot be reproduced safely from disk
(``asdf`` / ``mise``) FAIL OPEN — never invoked — leaving the pre-existing denial,
now *explained* by part B. Every failure path returns the original argv[0]
unchanged: resolution never changes *what* runs except to strip a shim.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# A resolved path under one of these directory segments is a version-manager
# shim whose launch machinery forks. Only managers we can resolve SAFELY from
# on-disk layout (a ``versions/<v>/bin`` tree) are listed; asdf/mise are
# deliberately absent so they fail open rather than being invoked (see docstring).
_SHIM_MARKER = "/shims/"

# manager marker segment → (env var for its root, default root under $HOME).
# Only managers resolvable SAFELY from a ``versions/<v>/bin`` layout are listed.
# asdf/mise are deliberately ABSENT: they are recognized as shims (so we fail
# open rather than run them) but never resolved, because reproducing their
# selection would mean reading configs a manager may template-``exec()``.
_RESOLVABLE_MANAGERS: tuple[tuple[str, str, str], ...] = (
    ("/.pyenv/", "PYENV_ROOT", ".pyenv"),
    ("/pyenv/", "PYENV_ROOT", ".pyenv"),
    ("/.rbenv/", "RBENV_ROOT", ".rbenv"),
    ("/rbenv/", "RBENV_ROOT", ".rbenv"),
)

# A valid version token: begins alphanumeric, then word/dot/dash/plus only. No
# path separators, no leading dot — so an attacker-supplied version string can
# never traverse out of ``<root>/versions/``.
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")

# ``.python-version`` / global ``version`` may list several versions; the first
# is primary. ``system`` means "use the OS binary, not a managed one".
_NOT_A_MANAGED_VERSION = frozenset({"system"})


def _resolvable_manager_root(path: str) -> str | None:
    """If *path* is a shim of a manager we resolve from disk, return that
    manager's root directory; else None."""
    if _SHIM_MARKER not in path:
        return None
    for marker, env_var, default_dir in _RESOLVABLE_MANAGERS:
        if marker in path:
            root = os.environ.get(env_var)
            if root:
                return root
            return str(Path.home() / default_dir)
    return None


# A version file is a handful of bytes. A ``.python-version`` sits in the
# agent/tool-writable cwd, so cap the read: an attacker who plants a multi-GB
# regular file (or a symlink to one) must not turn resolution into a memory DoS.
# The version token lives at the very start, so the head is all we ever need.
_VERSION_FILE_READ_CAP = 4096


def _first_token(text: str) -> str | None:
    """First whitespace/colon-free token of the first non-empty line of *text*,
    or None. (``PYENV_VERSION`` uses ``:`` to list multiple; files use lines.)"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return line.split(":")[0].split()[0]
    return None


def _read_version_file(path: Path) -> str | None:
    """First token of *path*'s head, or None. Bounded read (a version file is
    tiny) so an attacker-planted huge file cannot DoS resolution; ``is_file()``
    already excludes devices/FIFOs, so a symlink to ``/dev/zero`` never reaches
    here."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            head = fh.read(_VERSION_FILE_READ_CAP)
    except OSError:
        return None
    return _first_token(head.decode("utf-8", errors="replace"))


def _selected_version(root: str, cwd: str | None) -> str | None:
    """The version a pyenv/rbenv-style manager would select, read as plain data:
    ``PYENV_VERSION`` env → nearest ``.python-version`` walking up from *cwd* →
    global ``<root>/version``. Never executes anything."""
    env_v = os.environ.get("PYENV_VERSION") or os.environ.get("RBENV_VERSION")
    if env_v and (tok := _first_token(env_v)):
        return tok

    start = Path(cwd or os.getcwd())
    try:
        start = start.resolve()
    except OSError:
        return None
    for parent in (start, *start.parents):
        for fname in (".python-version", ".ruby-version"):
            if (tok := _read_version_file(parent / fname)) is not None:
                return tok

    return _read_version_file(Path(root) / "version")


def resolve_real_executable(
    argv0: str,
    *,
    env_path: str | None = None,
    cwd: str | None = None,
) -> str:
    """Return an absolute path to run in place of *argv0*, stripping a version-
    manager shim indirection when it can be resolved SAFELY from disk. Fail-open:
    returns the plain PATH resolution (or *argv0* unchanged) otherwise.

    *env_path* is the ``PATH`` the sandbox child will see (so resolution matches
    what the child would resolve); *cwd* is the child's working directory (so the
    manager's per-directory version file is read from the right place). No
    subprocess is ever spawned — the manager's on-disk layout is read as data.
    """
    found = shutil.which(argv0, path=env_path)
    if found is None:
        # Not on PATH — nothing to resolve; hand back the original so the backend
        # produces its normal "not found" error (unchanged behavior).
        return argv0

    if _SHIM_MARKER not in found and _SHIM_MARKER not in os.path.realpath(found):
        # A real binary already — return its absolute path (no shim indirection).
        return found

    root = _resolvable_manager_root(found) or _resolvable_manager_root(os.path.realpath(found))
    if root is None:
        # A shim of a manager we deliberately do not resolve (asdf/mise — never
        # invoked, its config can exec) or an unattributable shim: fail open to
        # the shim path, leaving the denial for part B to explain.
        return found

    version = _selected_version(root, cwd)
    if version is None or version in _NOT_A_MANAGED_VERSION:
        return found
    if not _VERSION_TOKEN.match(version):
        # Attacker-crafted / templated version string — never build a path from it.
        return found

    prog = os.path.basename(argv0)
    candidate = os.path.join(root, "versions", version, "bin", prog)
    # Defense-in-depth: the built path must stay under ``<root>/versions/`` (the
    # token regex already forbids separators, this guards against symlink games).
    versions_root = os.path.realpath(os.path.join(root, "versions"))
    if not os.path.realpath(candidate).startswith(versions_root + os.sep):
        return found
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return found
