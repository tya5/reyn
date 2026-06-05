"""Permission-gated file I/O for safe-mode python preprocessor / postprocessor steps.

FP-0042 — replaces ``reyn.api.unsafe.file`` for stdlib (and any user skill
that wants to opt into the Reyn permission model). Every file operation
goes through the path-declaration check that the calling skill's
``skill.md`` opted into; reads / writes outside the declared paths raise
:class:`PermissionError` and the step fails with a structured error.

Public surface
--------------

High-level (drop-in replacement for ``reyn.api.unsafe.file``):

- :func:`read(path, *, encoding="utf-8")` → str
- :func:`write(path, content, *, encoding="utf-8")` → None
- :func:`write_atomic(path, content, *, encoding="utf-8")` → None
- :func:`glob(pattern)` → list[str]
- :func:`exists(path)` → bool
- :func:`stat(path)` → {size, mtime, mode}
- :func:`mkdir(path, *, parents=False, exist_ok=False)` → None
- :func:`delete(path, *, missing_ok=False)` → None

Low-level (Python-IO-compatible):

- :func:`open(path, mode="r", *, encoding="utf-8", **kwargs)` → real
  ``io.TextIOBase`` / ``io.BufferedIOBase``. Use when streaming or
  partial reads matter, or when handing a file-like to a stdlib parser
  (``csv.reader``, ``json.load``, ``for line in f``).

Permission model
----------------

The check happens at call boundary (read) or at open time (``open``).
After permission grants, ``read`` performs a single full read and
``open`` returns the real file object — meaning subsequent ``seek`` /
``read(n)`` calls on the returned object are NOT individually
permission-checked. The permission contract is "may read this path",
not "may read these bytes".

Permission context is injected by the python harness
(``src/reyn/kernel/_python_harness.py``) before the user step runs.
Calling these functions outside that context (= bare unit test, ad-hoc
script) raises a clear ``PermissionError`` explaining how to set up the
context — see :func:`_set_permission_context` below.

Glob semantics
--------------

``glob`` does not gate the pattern itself — path enumeration without
content read is a metadata-level operation endorsed by the
2026-05-15 R-PURE-MODE stdlib audit. Subsequent content reads of any
returned path still go through :func:`read` / :func:`open` and are
permission-gated there.
"""
from __future__ import annotations

import builtins as _builtins
import glob as _glob_mod
import os as _os
import tempfile as _tempfile
from typing import IO, Any

# ── Internal state ─────────────────────────────────────────────────────────
#
# These three module-globals are set once at python harness start-up via
# :func:`_set_permission_context`. The values then govern every read /
# write / open call made by the user's python step against this module.

_read_paths: tuple[str, ...] = ()
_write_paths: tuple[str, ...] = ()
_context_initialised: bool = False

# #571 collapse arc Phase 2 / realignment: canonical paths whose write
# must go through a specific op handler (= ``index_drop`` for
# ``.reyn/index/sources.yaml``, transitionally) or, for
# ``.reyn/approvals.yaml``, the runtime approval-decision flow (#1199).
# Listing one of these in ``_write_paths`` via a parent directory
# (e.g. ``.reyn/``) is no longer enough; the path must appear in
# ``_write_paths`` *exactly* (= via an explicit ``file.write: [{path:
# ...}]`` decl, or via the bool-axis compat shim that auto-expands to
# the same entry).
#
# Protect-at-use migration: ``.reyn/mcp.yaml`` and ``.reyn/cron.yaml``
# were REMOVED from this set — using a server (``require_mcp``) / firing
# a cron job (gated, user-launched scheduler) is gated downstream, so the
# config-write carve-out is redundant. See the parent permissions.py note.
#
# #1199 (safe.file side): ``.reyn/approvals.yaml`` is the persisted
# approval store, written ONLY via the gated approval-decision mechanism.
# Without this carve-out on the safe.file enforcement path, a safe-mode
# python step could inject an approval via the broad ``.reyn/`` zone —
# bypassing the user-approval gate + audit (the subprocess always receives
# ``.reyn/`` in its write_paths, see preprocessor_executor). The parent
# permissions.py gate alone did not cover this enforcement path.
#
# Mirrors ``reyn.permissions.permissions._CANONICAL_PROTECTED_WRITE_PATHS``
# — keep the two lists in sync (drift-guarded by
# ``test_canonical_protected_lists_stay_in_sync``). They live in two
# modules because this one runs in the python-harness subprocess where
# importing the parent's permissions module is not always available.
_CANONICAL_PROTECTED_WRITE_PATHS = (
    ".reyn/index/sources.yaml",
    ".reyn/approvals.yaml",
)


def _set_permission_context(
    *,
    read_paths: list[str] | tuple[str, ...] | None = None,
    write_paths: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Wire permission paths into this module.

    Called by :mod:`reyn.kernel._python_harness` before the user step
    runs. Tests that exercise the file API directly may call this to
    establish a controlled context; production code should not.

    Idempotent: calling this overwrites the previous context. Passing
    ``None`` for either argument leaves that paths list empty (= every
    read / write is denied).
    """
    global _read_paths, _write_paths, _context_initialised
    _read_paths = tuple(read_paths or ())
    _write_paths = tuple(write_paths or ())
    _context_initialised = True


def _is_under(path: str, allowed: tuple[str, ...]) -> bool:
    """Return True if ``path`` resolves under any of ``allowed`` paths.

    Each ``allowed`` entry is normalised via ``os.path.abspath`` and
    treated as a directory-or-file prefix. ``path`` is normalised the
    same way, so ``../foo`` style escapes are caught by abspath
    expansion.

    Behaviour notes:
      - ``""`` (= empty string) in ``allowed`` matches nothing.
      - An exact-match prefix at a directory boundary counts (= the
        ``+ os.sep`` guard prevents ``/foo/bar`` matching ``/foo/barbaz``).
    """
    abs_path = _os.path.abspath(path)
    for entry in allowed:
        if not entry:
            continue
        abs_entry = _os.path.abspath(entry)
        if abs_path == abs_entry:
            return True
        # Treat allowed entries as prefixes only at directory boundaries.
        if abs_path.startswith(abs_entry + _os.sep):
            return True
    return False


def _check_read(path: str) -> None:
    if not _context_initialised:
        raise PermissionError(
            "reyn.safe.file: permission context not initialised. This "
            "module must be invoked from a PythonRunner-managed safe-mode "
            "step; bare-process use requires calling "
            "_set_permission_context(read_paths=..., write_paths=...) "
            f"first (read attempted: {path!r})."
        )
    if not _is_under(path, _read_paths):
        raise PermissionError(
            f"reyn.safe.file: read from {path!r} is not in the declared "
            f"read_paths {list(_read_paths)}. Declare it in skill.md "
            f"frontmatter:\n"
            f"  permissions:\n"
            f"    file.read:\n"
            f"      - path: {path}\n"
            f"        scope: just_path\n"
        )


def _is_canonical_protected_write(path: str) -> bool:
    """Return True if ``path`` resolves to one of the #571 protected paths.

    These paths are normally writable via the broad ``.reyn/`` default
    zone, but the collapse-arc Phase 2 carve-out requires they be listed
    explicitly in ``_write_paths`` (not via a parent-directory prefix).
    """
    abs_path = _os.path.abspath(path)
    cwd = _os.getcwd()
    for rel in _CANONICAL_PROTECTED_WRITE_PATHS:
        if abs_path == _os.path.abspath(_os.path.join(cwd, rel)):
            return True
    return False


def _is_explicitly_listed(path: str, allowed: tuple[str, ...]) -> bool:
    """Return True iff ``path`` exactly matches one of ``allowed`` entries.

    Stricter than :func:`_is_under` — does not accept parent-directory
    prefix matches. Used to enforce the #571 protected-paths exception.
    """
    abs_path = _os.path.abspath(path)
    for entry in allowed:
        if entry and abs_path == _os.path.abspath(entry):
            return True
    return False


def _check_write(path: str) -> None:
    if not _context_initialised:
        raise PermissionError(
            "reyn.safe.file: permission context not initialised "
            f"(write attempted: {path!r})."
        )
    # #571 collapse arc Phase 2: canonical protected paths require an
    # explicit listing (= not the broad ``.reyn/`` parent-dir match).
    if _is_canonical_protected_write(path):
        if not _is_explicitly_listed(path, _write_paths):
            raise PermissionError(
                f"reyn.safe.file: write to {path!r} requires an explicit "
                f"declaration — this is a canonical protected path "
                f"(#571) and is not covered by the broad ``.reyn/`` "
                f"default write zone. Declare it directly:\n"
                f"  permissions:\n"
                f"    file.write:\n"
                f"      - path: {path}\n"
                f"        scope: just_path\n"
                f"(or use the corresponding bool axis — `mcp_install` / "
                f"`mcp_drop_server` / `cron_register` / `index_drop` — "
                f"which auto-expands to the same explicit entry.)"
            )
        return
    if not _is_under(path, _write_paths):
        raise PermissionError(
            f"reyn.safe.file: write to {path!r} is not in the declared "
            f"write_paths {list(_write_paths)}. Declare it in skill.md "
            f"frontmatter:\n"
            f"  permissions:\n"
            f"    file.write:\n"
            f"      - path: {path}\n"
            f"        scope: just_path\n"
        )


# ── High-level API (drop-in for reyn.api.unsafe.file) ───────────────────────


def read(path: str, *, encoding: str = "utf-8") -> str:
    """Read and return the text contents of ``path``.

    Permission-checked: ``path`` must resolve under one of the
    declared ``read_paths``. Raises :class:`PermissionError` otherwise.
    """
    _check_read(path)
    with _builtins.open(path, encoding=encoding) as f:
        return f.read()


def write(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path``, replacing any existing content.

    Permission-checked: ``path`` must resolve under one of the
    declared ``write_paths``. Raises :class:`PermissionError` otherwise.
    """
    _check_write(path)
    with _builtins.open(path, "w", encoding=encoding) as f:
        f.write(content)


def write_atomic(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically via tempfile + ``os.replace``.

    Permission-checked: ``path`` must resolve under one of the declared
    ``write_paths``. The temporary file is created in the same directory
    as ``path`` so the final ``os.replace`` is guaranteed atomic on
    POSIX filesystems.

    On any error during write, the temp file is unlinked and the
    original ``path`` is left untouched. Use case: cursor files, lock
    files, and other small-file writes where crash-safety matters.
    """
    _check_write(path)
    dir_path = _os.path.dirname(_os.path.abspath(path)) or "."
    fd, tmp = _tempfile.mkstemp(prefix=".reyn_safe_atomic_", dir=dir_path)
    try:
        with _os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        _os.replace(tmp, path)
    except Exception:
        try:
            _os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def glob(pattern: str) -> list[str]:
    """Return paths matching ``pattern`` (recursive ``**`` supported).

    Glob is metadata-level enumeration, not content read; the
    2026-05-15 R-PURE-MODE stdlib audit endorsed unrestricted glob
    for safe-mode python steps. Subsequent reads of any returned path
    still go through :func:`read` / :func:`open` and are
    permission-gated there.

    Result is sorted for stable iteration.
    """
    return sorted(_glob_mod.glob(pattern, recursive=True))


def exists(path: str) -> bool:
    """Return whether ``path`` exists.

    Permission-checked as a read (= existence is a metadata observation
    that requires the same authority as reading the file).
    """
    _check_read(path)
    return _os.path.exists(path)


def stat(path: str) -> dict[str, Any]:
    """Return a simplified ``{size, mtime, mode}`` dict for ``path``.

    Permission-checked as a read.
    """
    _check_read(path)
    st = _os.stat(path)
    return {"size": st.st_size, "mtime": st.st_mtime, "mode": st.st_mode}


def mkdir(
    path: str,
    *,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    """Create directory ``path``.

    Permission-checked as a write — ``path`` must resolve under one of
    the declared ``write_paths``. Mirrors :meth:`pathlib.Path.mkdir`:

    - ``parents=True`` creates missing intermediate directories;
    - ``exist_ok=True`` does not raise when the directory already exists.

    Raises :class:`PermissionError` when ``path`` is outside the declared
    write zone, :class:`FileExistsError` when the directory exists and
    ``exist_ok=False``, and :class:`FileNotFoundError` when a parent
    directory is missing and ``parents=False``.
    """
    _check_write(path)
    if parents:
        _os.makedirs(path, exist_ok=exist_ok)
        return
    try:
        _os.mkdir(path)
    except FileExistsError:
        if not exist_ok:
            raise


def delete(path: str, *, missing_ok: bool = False) -> None:
    """Remove file ``path``.

    Permission-checked as a write — the path must resolve under one of
    the declared ``write_paths``. Mirrors :meth:`pathlib.Path.unlink`:
    ``missing_ok=True`` swallows :class:`FileNotFoundError`. Directory
    removal isn't part of the safe surface — explicit unsafe-mode is the
    right gate when needed.
    """
    _check_write(path)
    try:
        _os.unlink(path)
    except FileNotFoundError:
        if not missing_ok:
            raise


# ── Low-level API (Python-IO-compatible) ────────────────────────────────────


def open(  # noqa: A001 — intentional shadowing of builtin in this module's surface
    path: str,
    mode: str = "r",
    *,
    encoding: str = "utf-8",
    **kwargs: Any,
) -> IO[Any]:
    """Permission-gated equivalent of ``builtins.open``.

    Returns a real ``io.TextIOBase`` / ``io.BufferedIOBase``. Mode
    determines which permission applies: any mode containing ``w``,
    ``a``, ``x``, or ``+`` requires write; otherwise read.

    Once permission grants, the returned object behaves identically to
    the result of ``builtins.open(path, mode, encoding=encoding,
    **kwargs)``. Subsequent ``seek`` / ``read(n)`` / iteration on the
    returned object are NOT per-call permission-checked — the contract
    is "may read this path".

    ``builtins.open`` itself is banned in safe-mode python by the AST
    validator; this function is the permission-gated gateway that
    replaces it.
    """
    needs_write = any(c in mode for c in ("w", "a", "x", "+"))
    if needs_write:
        _check_write(path)
    else:
        # Default to read for empty / read-only modes ("r", "rb", "rt", ...).
        _check_read(path)
    return _builtins.open(path, mode, encoding=encoding, **kwargs)
