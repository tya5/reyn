"""Workspace path helpers for ``unsafe``-mode python steps.

Scope A (= this FP): inspects the subprocess's current working
directory directly. Permission was granted at parent level when
the step's ``mode: unsafe`` was approved at startup; individual
calls are NOT audited per-invocation (= step-level audit only).
For finer audit see FP-0015 (deferred).
"""

from __future__ import annotations

import os as _os


def cwd() -> str:
    """Return the subprocess's current working directory.

    Step-level audit (Scope A) — see module docstring.
    """
    return _os.getcwd()


def path(*parts: str) -> str:
    """Join ``parts`` onto the subprocess's current working directory.

    Step-level audit (Scope A) — see module docstring.
    """
    return _os.path.join(cwd(), *parts)


def list_artifacts() -> list[str]:
    """List entries in the subprocess's current working directory.

    Step-level audit (Scope A) — see module docstring.
    """
    return _os.listdir(cwd())
