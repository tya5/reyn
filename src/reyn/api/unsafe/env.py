"""Environment-variable helpers for ``unsafe``-mode python steps.

Scope A (= this FP): reads ``os.environ`` directly. Permission was
granted at parent level when the step's ``mode: unsafe`` was
approved at startup; individual calls are NOT audited per-invocation
(= step-level audit only). For finer audit see FP-0015 (deferred).
"""

from __future__ import annotations

import os as _os


def get(key: str, default: str | None = None) -> str | None:
    """Return ``os.environ[key]`` or ``default`` if unset.

    Step-level audit (Scope A) — see module docstring.
    """
    return _os.environ.get(key, default)
