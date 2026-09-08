"""Tier 2: `_derivation_cache.cached_derivation`'s `on_evict` hook (#5981).

Before this, a cached VALUE that itself owned a resource (Seatbelt's `.sb`
profile path) had no exit at all: the weakref callback the module already
ran on policy collection cleared only its own bookkeeping dicts
(`_CACHE`/`_REFS`), never anything the cached value itself pointed at. These
tests exercise the generic hook directly — no filesystem, no SandboxPolicy —
`_derivation_cache.py` itself takes any hashable-by-identity key and any
`compute`/`on_evict` pair.
"""
from __future__ import annotations

import gc

import pytest

from reyn.security.sandbox import _derivation_cache
from reyn.security.sandbox._derivation_cache import cached_derivation
from reyn.security.sandbox.policy import SandboxPolicy


@pytest.fixture(autouse=True)
def _reset_derivation_cache():
    """Isolate each test from cache state any other test (in this file or a
    sibling like test_sandbox_seatbelt.py) may have left behind — same
    rationale as that file's own fixture of the same name."""
    _derivation_cache._reset_cache_for_tests()
    yield
    _derivation_cache._reset_cache_for_tests()


def test_on_evict_fires_with_the_cached_value_when_the_policy_is_collected():
    """Tier 2: the hook this issue adds — `on_evict` runs with the cached
    VALUE (not the policy) at the exact moment the keying policy object is
    garbage-collected, the same event that already clears the in-memory
    cache entry."""
    evicted: list[str] = []

    def _make_policy_and_cache() -> None:
        # Confined to its own frame so the local `policy` binding is gone
        # the instant this function returns — CPython drops a function's
        # locals on return, which is what makes the object collectible
        # without an explicit `del` at the call site.
        policy = SandboxPolicy(write_paths=[])
        value = cached_derivation(
            "test-backend", policy, lambda: "computed-value",
            on_evict=evicted.append,
        )
        assert value == "computed-value"

    _make_policy_and_cache()
    # CPython frees a refcount-only (non-cyclic) object the instant its last
    # reference drops — the local `policy` binding inside the confined frame
    # above — so eviction typically already ran by this point; `gc.collect()`
    # is the portable way to demand it regardless of that implementation
    # detail, not a wait for something that might not have happened yet.
    gc.collect()

    assert evicted == ["computed-value"]


def test_on_evict_does_not_fire_while_the_policy_is_still_alive():
    """Tier 2: strip-falsify's counterpart — the SAME cache entry, but the
    policy is kept alive by the test's own local variable. `gc.collect()`
    must not evict a live-referenced key; if it did, this would also assert
    a repeat `cached_derivation` call started recomputing instead of
    hitting the cache."""
    evicted: list[str] = []
    calls = {"n": 0}

    def _compute() -> str:
        calls["n"] += 1
        return f"computed-{calls['n']}"

    policy = SandboxPolicy(write_paths=[])
    first = cached_derivation("test-backend", policy, _compute, on_evict=evicted.append)

    gc.collect()
    assert evicted == []

    second = cached_derivation("test-backend", policy, _compute, on_evict=evicted.append)
    assert second == first
    assert calls["n"] == 1  # _compute ran exactly once — the cache hit, not a re-derive


def test_cached_derivation_without_on_evict_is_unaffected_by_eviction():
    """Tier 2: backward-compat — a caller passing no `on_evict` (every
    caller before #5981) gets byte-identical behavior: eviction firing with
    nothing to call must not raise, and the module keeps working normally
    for the NEXT policy afterward (asserted via `cached_derivation`'s own
    return value, not `_derivation_cache`'s private dicts — #4864 Rule 8)."""
    def _make_policy_and_cache() -> None:
        policy = SandboxPolicy(write_paths=[])
        cached_derivation("test-backend", policy, lambda: "v")

    _make_policy_and_cache()  # eviction (on_evict=None) runs somewhere in here
    gc.collect()  # portable demand, in case it hasn't run yet

    # A later, unrelated policy still computes and caches correctly — the
    # module's own state was not corrupted by an eviction with no on_evict.
    later_policy = SandboxPolicy(write_paths=[])
    assert cached_derivation("test-backend", later_policy, lambda: "w") == "w"
