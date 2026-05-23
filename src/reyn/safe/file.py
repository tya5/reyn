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
from typing import IO, Any

# ── Internal state ─────────────────────────────────────────────────────────
#
# These three module-globals are set once at python harness start-up via
# :func:`_set_permission_context`. The values then govern every read /
# write / open call made by the user's python step against this module.

_read_paths: tuple[str, ...] = ()
_write_paths: tuple[str, ...] = ()
_context_initialised: bool = False


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


def _check_write(path: str) -> None:
    if not _context_initialised:
        raise PermissionError(
            "reyn.safe.file: permission context not initialised "
            f"(write attempted: {path!r})."
        )
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
