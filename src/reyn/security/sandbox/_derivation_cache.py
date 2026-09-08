"""Process-wide, identity-keyed cache for a backend's policy-DERIVED
representation (#4434 stage 1).

The contract this exists for lives on ``SandboxBackend`` (see
``backend.py``'s ``session_artifact_outside_write_scope`` docstring for the
security precondition that gates writing a derivation to disk): a policy is
already a session constant (``resolve_sandbox_policy`` is called from only 3
non-per-op sites — config/loader.py, router_op_context.py,
tools/exec.py — and the resulting object is stored on a session-scoped
context and passed BY REFERENCE into every ``wrap_command``/``run`` call), so
re-deriving the SAME representation from the SAME policy object on every call
is pure waste. This module is where "derive once per (backend, policy)" is
actually enforced, mirroring ``self_test.py``'s own process-global probe
cache — the SAME structural reason applies here: ``get_default_backend()``
builds a FRESH backend instance per call (including once per op, from
``sandboxed_exec.py``), so a cache living on ``self`` never survives across
calls; it has to live at module scope, in ONE place both callers share.

Keyed by ``(backend_name, id(policy))``, not by policy CONTENT — a plain
``SandboxPolicy`` dataclass is unhashable (list fields, no ``frozen=True``),
and the real call pattern above means identity is both sufficient (the same
object really is reused across a session) and simpler (no need to serialize
a policy just to compute a cache key). Identity keys age out via a
``weakref`` callback on *policy* itself rather than TTL/size eviction — once
the policy object is garbage-collected, its cache entry is removed in the
SAME step, so a future object that happens to be allocated at the same
``id()`` can never collide with a stale entry.

#5981: eviction was in-memory only — a *compute* that writes a disk artifact
(Seatbelt's cached ``.sb`` profile) had no exit at all, because the callback
above dropped the dict entries and nothing else. ``on_evict`` (optional,
below) ties a caller's own disk cleanup to the SAME weakref firing that
already exists for the in-memory entry, rather than inventing a second,
separate lifetime for the on-disk copy — the two were always meant to expire
together; only one half of that was wired.
"""
from __future__ import annotations

import threading
import weakref
from typing import Any, Callable, TypeVar

from .policy import SandboxPolicy

T = TypeVar("T")

_CACHE: dict[tuple[str, int], Any] = {}
# Keeps each policy's weakref ALIVE — a `weakref.ref(obj, cb)` whose return
# value is discarded is, in CPython, itself immediately collected (nothing
# holds it), which silently disarms *cb*: confirmed live, a bare
# `weakref.ref(f, cb); del f; gc.collect()` never fires `cb` unless the ref
# object itself is kept somewhere. This dict is that "somewhere" — its own
# entry is removed by the SAME evictor callback that clears ``_CACHE``.
_REFS: dict[tuple[str, int], "weakref.ref[SandboxPolicy]"] = {}
# #5981: the caller-supplied cleanup for a cached VALUE, fired by the same
# evictor that clears `_CACHE`/`_REFS` for this key — absent for callers with
# nothing to release beyond the dict entry itself (the pre-#5981 shape).
_ON_EVICT: dict[tuple[str, int], "Callable[[Any], None]"] = {}
_LOCK = threading.Lock()


def cached_derivation(
    backend_name: str,
    policy: SandboxPolicy,
    compute: "Callable[[], T]",
    *,
    on_evict: "Callable[[T], None] | None" = None,
) -> T:
    """Return the cached derivation for ``(backend_name, policy)``, computing
    it via *compute* exactly once per (backend, policy object) per process.

    *compute* runs under the lock — derivations are cold-path, cheap-to-rare
    operations (a JSON dump; a temp-file write on a cache miss), so holding
    the lock across it trades a small amount of contention for never racing
    two callers into computing (and, for Seatbelt, WRITING) the same
    derivation twice.

    ``on_evict`` (#5981), when given, is called with the cached VALUE at the
    exact moment *policy* is garbage-collected and its entry is dropped —
    the hook a caller whose *compute* result is a disk artifact (a path, a
    file handle) needs to release that artifact on the SAME lifecycle event
    that already ends the in-memory entry, instead of the artifact outliving
    it with no eviction event of its own. Keyword-only and optional: a
    caller with nothing to release (the pre-#5981 shape) passes nothing and
    gets byte-identical behavior.
    """
    key = (backend_name, id(policy))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        value = compute()
        _CACHE[key] = value
        if on_evict is not None:
            _ON_EVICT[key] = on_evict
        _REFS[key] = weakref.ref(policy, _evictor(key))
        return value


def _evictor(key: tuple[str, int]) -> "Callable[[Any], None]":
    def _evict(_ref: "Any") -> None:
        with _LOCK:
            value = _CACHE.pop(key, None)
            _REFS.pop(key, None)
            on_evict = _ON_EVICT.pop(key, None)
        # #5981: run the caller's cleanup OUTSIDE `_LOCK` — an on_evict that
        # re-enters `cached_derivation` (a different key; the same key can't
        # recur, this entry is already gone) would otherwise deadlock on the
        # same non-reentrant lock this callback was invoked from.
        if on_evict is not None:
            on_evict(value)

    return _evict


def _reset_cache_for_tests() -> None:
    """Test hook: drop the process-global derivation cache.

    Does NOT fire any `on_evict` callback — this is a bookkeeping reset, not
    a real eviction; a test that needs the disk-cleanup side effect exercised
    should let the policy it created go out of scope and be collected."""
    with _LOCK:
        _CACHE.clear()
        _REFS.clear()
        _ON_EVICT.clear()


def _cache_size_for_tests() -> int:
    """Test hook: current entry count, for asserting eviction actually ran."""
    with _LOCK:
        return len(_CACHE)
