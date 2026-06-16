"""Filesystem helpers for ``unsafe``-mode python steps.

Scope A (= this FP): runs inside the python step's subprocess and
calls stdlib I/O directly. Permission was granted at parent level
when the step's ``mode: unsafe`` was approved at startup;
individual calls are NOT audited per-invocation (= step-level
audit only). For finer audit see FP-0015 (deferred).
"""

from __future__ import annotations

import glob as _glob
import os as _os


def read(path: str, *, encoding: str = "utf-8") -> str:
    """Read and return the text contents of ``path``.

    Step-level audit (Scope A) — see module docstring.
    """
    with open(path, encoding=encoding) as f:
        return f.read()


def write(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path``, replacing any existing content.

    Step-level audit (Scope A) — see module docstring.
    """
    with open(path, "w", encoding=encoding) as f:
        f.write(content)


def delete(path: str) -> None:
    """Delete the file at ``path``.

    Step-level audit (Scope A) — see module docstring.
    """
    _os.remove(path)


def glob(pattern: str) -> list[str]:
    """Return paths matching ``pattern`` (recursive ``**`` supported).

    Result is sorted for stable iteration. Step-level audit (Scope A)
    — see module docstring.
    """
    return sorted(_glob.glob(pattern, recursive=True))


def exists(path: str) -> bool:
    """Return whether ``path`` exists.

    Step-level audit (Scope A) — see module docstring.
    """
    return _os.path.exists(path)


def stat(path: str) -> dict:
    """Return a simplified ``{size, mtime, mode}`` dict for ``path``.

    Step-level audit (Scope A) — see module docstring.
    """
    st = _os.stat(path)
    return {"size": st.st_size, "mtime": st.st_mtime, "mode": st.st_mode}
