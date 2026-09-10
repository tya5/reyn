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
above dropped the dict entries and nothing else. ``on_evict`` ties a caller's
own disk cleanup to the SAME weakref firing that already exists for the
in-memory entry, rather than inventing a second, separate lifetime for the
on-disk copy — the two were always meant to expire together; only one half
of that was wired.

#5981 co-vet (architect): tying the on-disk lifetime SOLELY to *policy*'s own
GC is a real fix but an incomplete contract — (1) the consumer of the derived
artifact is whatever `compute()` produced it FOR (a running `sandbox-exec`),
not *policy* itself, so "when does GC happen" is an implementation detail,
not a promise this module makes; (2) `cached_derivation` is called once per
CHECKOUT — the SAME key can have several live callers at once (two
`wrap_command()` calls sharing one policy) — and nothing counted how many
outstanding checkouts existed, so an eager, GC-timing-only eviction could in
principle race a still-live SECOND checkout. ``release_derivation`` (below)
is the fix: an EXPLICIT decrement a caller makes when it is done with its own
checkout, independent of whether *policy* itself has died yet — the weakref
callback remains the FLOOR (fires unconditionally, ignoring any outstanding
count, because once *policy* itself is unreachable nothing could call
``release_derivation`` for it ever again regardless), not the only path.
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
# #5981: the caller-supplied cleanup for a cached VALUE, fired by whichever
# of `_evictor`/`release_derivation` empties this key first — absent for
# callers with nothing to release beyond the dict entry itself (the
# pre-#5981 shape).
_ON_EVICT: dict[tuple[str, int], "Callable[[Any], None]"] = {}
# #5981 co-vet: outstanding-checkout count per key — incremented on every
# `cached_derivation` call (hit AND miss; each call is a NEW caller's claim
# on the value, not just the first one that computed it), decremented by
# `release_derivation`. A key present here is, by construction, also present
# in `_CACHE` (both are only ever populated/cleared together under `_LOCK`)
# — so `key in _CACHE` and `key in _REFCOUNT` never disagree about whether
# an entry is live; nothing reads one without the other under the same lock.
#
# `id()` reuse against a key still IN this dict cannot happen: a caller
# needs a live reference to *policy* to compute `key` at all (both here and
# in `release_derivation`), and holding that reference is exactly what
# keeps the OLD object un-collectable — so `_evictor`'s callback (the only
# thing that would free `id(policy)` for reuse) cannot have fired yet.
_REFCOUNT: dict[tuple[str, int], int] = {}
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

    ``on_evict`` (#5981), when given, is called with the cached VALUE the
    moment the LAST outstanding checkout goes away — either every caller
    that received this value has called :func:`release_derivation`, or
    *policy* itself is garbage-collected (whichever comes first) — the hook
    a caller whose *compute* result is a disk artifact (a path, a file
    handle) needs to release that artifact, instead of it outliving every
    consumer with no eviction event of its own. Keyword-only and optional: a
    caller with nothing to release (the pre-#5981 shape) passes nothing and
    gets byte-identical behavior.

    Each call — hit or miss — is its OWN checkout and increments the
    key's live count; a caller that intends to eventually release must call
    :func:`release_derivation` exactly once per `cached_derivation` call it
    made (not once total) — see that function's own docstring.
    """
    key = (backend_name, id(policy))
    with _LOCK:
        if key in _CACHE:
            _REFCOUNT[key] += 1
            return _CACHE[key]
        value = compute()
        _CACHE[key] = value
        _REFCOUNT[key] = 1
        if on_evict is not None:
            _ON_EVICT[key] = on_evict
        _REFS[key] = weakref.ref(policy, _evictor(key))
        return value


def release_derivation(backend_name: str, policy: SandboxPolicy) -> None:
    """Release ONE checkout of ``(backend_name, policy)`` obtained from a
    prior :func:`cached_derivation` call (#5981 co-vet).

    Decrements the key's live-checkout count; when it reaches zero, this
    call — not a later GC of *policy* — is what runs ``on_evict`` and drops
    the cache entry. A key with a still-positive count after this call is
    left fully alone: another live checkout (a second `wrap_command()` call
    sharing the same policy, say) may still need the cached value, and NEITHER
    this function nor the weakref evictor ever removes an entry while ANY
    checkout of it remains outstanding.

    A no-op if *policy* was never checked out under this *backend_name*, or
    every checkout already released (idempotent from the CALLER's side —
    but each `cached_derivation` call still needs its OWN matching release;
    calling this twice for what was only ONE checkout under-counts and can
    evict while a genuinely separate checkout is still live, so a caller
    must track "have I already released THIS checkout" itself — see
    Seatbelt's ``wrap_command._cleanup`` for the idempotency guard that
    entails)."""
    key = (backend_name, id(policy))
    with _LOCK:
        if key not in _REFCOUNT:
            return
        _REFCOUNT[key] -= 1
        if _REFCOUNT[key] > 0:
            return
        value = _CACHE.pop(key, None)
        _REFS.pop(key, None)
        _REFCOUNT.pop(key, None)
        on_evict = _ON_EVICT.pop(key, None)
    # Outside `_LOCK` — see `_evictor`'s own comment on why.
    if on_evict is not None:
        on_evict(value)


def _evictor(key: tuple[str, int]) -> "Callable[[Any], None]":
    def _evict(_ref: "Any") -> None:
        with _LOCK:
            # #5981 co-vet: policy itself just died — this is the FLOOR, not
            # the primary release path, so it evicts UNCONDITIONALLY, ignoring
            # any positive `_REFCOUNT` left over. That's correct, not a race:
            # every remaining "checkout" of this key was reachable only
            # through *policy* (nothing else can compute this key), and
            # *policy* just became unreachable — by definition nothing can
            # ever call `release_derivation` for it again, so a leftover
            # positive count here would otherwise never reach zero on its
            # own. Real callers that DO explicitly release (Seatbelt's
            # `_cleanup`) normally reach zero via `release_derivation` first,
            # well before *policy* is ever collected; this path exists for
            # whatever never got explicitly released (a crash mid-checkout,
            # a caller that dropped everything without calling cleanup) —
            # exactly the safety-net role this callback always had.
            value = _CACHE.pop(key, None)
            _REFS.pop(key, None)
            _REFCOUNT.pop(key, None)
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
    should let the policy it created go out of scope and be collected, or
    call :func:`release_derivation` to force it explicitly."""
    with _LOCK:
        _CACHE.clear()
        _REFS.clear()
        _ON_EVICT.clear()
        _REFCOUNT.clear()


def _cache_size_for_tests() -> int:
    """Test hook: current entry count, for asserting eviction actually ran."""
    with _LOCK:
        return len(_CACHE)


def _outstanding_checkout_count_for_tests(backend_name: str, policy: SandboxPolicy) -> int:
    """Test hook: the live-checkout count for ``(backend_name, policy)`` —
    0 if never checked out or already fully released. #5939 PR-1's own
    witness (that :func:`~reyn.runtime.process_memory_release.
    release_reconstructable_caches` never touches this module) reads
    this snapshot-style function rather than ``_CACHE``/``_REFCOUNT``
    directly, per this repo's own testing policy on private state."""
    with _LOCK:
        return _REFCOUNT.get((backend_name, id(policy)), 0)
