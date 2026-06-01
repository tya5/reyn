"""Per-(model, call-shape) ``response_format`` capability cache (#1212 D5).

Caches whether a model accepts ``response_format`` for a given call shape so the
broad ``except Exception`` fallback in ``recorded_acompletion`` does not re-pay
the provider's 400 round-trip on every call after the first.

The capability is **call-shape specific**, not just per-model: e.g. Gemini
accepts ``response_format`` alone (json-mode) but rejects ``response_format``
*combined with* ``tools`` (the #1212 op-loop call). So the cache key includes
whether ``tools`` are present — ``(model, has_tools)`` — and the two cases are
recorded independently.

Pure optimization: behavior is unchanged (the fallback already handles the 400);
the cache only lets a known-incapable (model, shape) skip the doomed
``response_format`` attempt proactively. Process-local + thread-safe; cleared on
restart (a capability is stable for a model build, and re-probing once is cheap).
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
# (model, has_tools) -> response_format supported on that shape. Absent = unknown.
_cache: dict[tuple[str, bool], bool] = {}


def response_format_supported(model: str, *, has_tools: bool) -> bool | None:
    """Return cached support for ``response_format`` on (model, has_tools).

    ``None`` means "not yet probed" — the caller should attempt and record.
    """
    with _lock:
        return _cache.get((model, has_tools))


def record_response_format_support(model: str, *, has_tools: bool, supported: bool) -> None:
    """Record whether (model, has_tools) accepts ``response_format``."""
    with _lock:
        _cache[(model, has_tools)] = supported


def reset() -> None:
    """Clear the cache (test isolation; also valid at runtime to force re-probe)."""
    with _lock:
        _cache.clear()


def snapshot() -> dict[tuple[str, bool], bool]:
    """Return a copy of the current cache (read surface for tests/diagnostics)."""
    with _lock:
        return dict(_cache)
