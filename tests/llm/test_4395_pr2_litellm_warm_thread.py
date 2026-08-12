"""Tier 2: `ensure_litellm_ready_or_defer()` never blocks the calling
thread, and exactly ONE dedicated background thread owns retrying a
failed `import litellm` — not one thread per call (#4395 PR-2, owner
design).

THE BUG: a live owner repro (py-spy stack) caught the event-loop/UI
thread itself blocked inside the FIRST-EVER `import litellm` — which
transitively runs tiktoken's own un-timed-out network fetch at litellm's
OWN import time (#4395 PR-1 already fixed the no-fallback call sites'
own "wait on the UI loop" shape via `asyncio.to_thread`; this PR
addresses the sites that have a safe fallback and so should not wait on
litellm AT ALL). Moving the import "onto a worker thread" is not itself
the fix (Python's own import lock already serializes concurrent imports
of the SAME module safely) — the actual requirement is that a caller
with a fallback never blocks even briefly, and (verified necessary by
Python's own semantics: a module whose top-level code raises is evicted
from `sys.modules`, so a failed import is retried from scratch on the
next attempt) that a persistently-failing environment does not spawn an
unbounded number of worker threads, one per call.

Uses a REAL callable substituting for `reyn.llm.litellm_bootstrap.
ensure_litellm_ready` (`monkeypatch.setattr`, not `unittest.mock`) to
control its outcome deterministically — an interception of this
module's own function, not a mock of any object model.
"""
from __future__ import annotations

import threading
import time

import pytest

import reyn.llm.litellm_bootstrap as lb_mod
from reyn.llm.litellm_bootstrap import (
    LitellmWarmingInBackgroundError,
    ensure_litellm_ready_or_defer,
)


class _FakeLitellmModule:
    """A real, distinguishable stand-in for the module `ensure_litellm_
    ready()` would normally return on success — lets a test simulate "the
    import just succeeded" without performing a real import."""


def _let_worker_thread_finish_and_reap_it() -> None:
    """End-of-test helper: flips `_litellm_ready` True directly (as the
    real `ensure_litellm_ready()` only does after a genuine success) and
    waits for the CURRENT worker thread (if any) to notice and exit
    before returning. Every test that simulates a persistent FAILURE
    must call this before finishing, or the worker thread it started
    keeps polling for up to the test's own (possibly long) cooldown
    value — outliving the test, `monkeypatch`'s own auto-revert, and
    lingering as a `reyn-litellm-warm`-named thread `threading.
    enumerate()` would show to every LATER test in the same pytest
    process. The worker's own SHORT internal poll interval
    (`_LITELLM_WARM_POLL_SECONDS`, independent of whatever cooldown the
    test configured) means it notices this within a fraction of a second
    regardless of the test's own cooldown value."""
    lb_mod._litellm_ready = True
    thread = lb_mod._litellm_warm_thread
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread did not finish within the reap timeout"


@pytest.fixture(autouse=True)
def _clean_litellm_bootstrap_state():
    """Tier 2 hygiene: reset the module-global worker-thread handle and
    `_litellm_ready` flag for the duration of each test in this file,
    restoring the real pre-test state afterward — without this, a test
    running after litellm has genuinely, successfully imported earlier
    in the same pytest session would see `_litellm_ready` already True
    and never exercise the not-yet-ready path at all."""
    prior_thread = lb_mod._litellm_warm_thread
    if prior_thread is not None and prior_thread.is_alive():
        prior_thread.join(timeout=5)
    lb_mod._litellm_warm_thread = None
    original_ready = lb_mod._litellm_ready
    lb_mod._litellm_ready = False
    try:
        yield
    finally:
        thread = lb_mod._litellm_warm_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        lb_mod._litellm_warm_thread = None
        lb_mod._litellm_ready = original_ready


def test_repeated_calls_during_a_persistent_failure_never_block(monkeypatch):
    """Tier 2: THE core defect. Every call must return in effectively zero
    time, even while litellm keeps failing to import on every attempt."""
    def _always_failing_ensure_ready():
        return None  # matches the real function's own contract: never
        # raises, returns None on failure, does not flip `_litellm_ready`.

    monkeypatch.setattr(lb_mod, "ensure_litellm_ready", _always_failing_ensure_ready)

    start = time.monotonic()
    for _ in range(20):
        with pytest.raises(LitellmWarmingInBackgroundError):
            ensure_litellm_ready_or_defer()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"20 calls during a persistent failure took {elapsed:.3f}s — the "
        f"calling thread must never wait on the import attempt"
    )
    _let_worker_thread_finish_and_reap_it()


def test_exactly_one_worker_thread_regardless_of_call_volume(monkeypatch):
    """Tier 2: owner's condition — a persistently failing environment must
    not accumulate one thread per call. This is the test that would have
    failed against a respawn-per-caller design."""
    def _always_failing_ensure_ready():
        return None

    monkeypatch.setattr(lb_mod, "ensure_litellm_ready", _always_failing_ensure_ready)

    for _ in range(20):
        with pytest.raises(LitellmWarmingInBackgroundError):
            ensure_litellm_ready_or_defer()

    warm_threads = [t for t in threading.enumerate() if t.name == "reyn-litellm-warm"]
    # Exactly 1 thread, not "at most 1" — asserted by unpacking into exactly
    # one binding (canonical idiom: a wrong COUNT fails the unpack itself,
    # not a len(...) == N format pin).
    (single_thread,) = warm_threads
    assert single_thread.name == "reyn-litellm-warm"
    _let_worker_thread_finish_and_reap_it()


def test_a_success_is_picked_up_by_the_next_call(monkeypatch):
    """Tier 2: recovery — once the dedicated worker thread succeeds
    (litellm becomes ready), the NEXT deferred call must succeed
    normally, not keep raising."""
    fake_module = _FakeLitellmModule()

    def _succeeding_ensure_ready():
        lb_mod._litellm_ready = True
        return fake_module

    monkeypatch.setattr(lb_mod, "ensure_litellm_ready", _succeeding_ensure_ready)

    assert not lb_mod.is_litellm_ready()
    with pytest.raises(LitellmWarmingInBackgroundError):
        ensure_litellm_ready_or_defer()

    # Wait for the worker thread to actually run and succeed — bounded,
    # unconditional wait on the condition itself, not a fixed sleep.
    thread = lb_mod._litellm_warm_thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive(), "worker thread did not finish within the join timeout"

    assert lb_mod.is_litellm_ready()
    result = ensure_litellm_ready_or_defer()  # must NOT raise now
    assert result is fake_module


def test_already_ready_case_never_touches_the_worker_thread(monkeypatch):
    """Tier 2: accept-side — when litellm is ALREADY ready, this function
    must behave like the plain `ensure_litellm_ready()` and never spawn a
    worker thread at all (the common case, after the first successful
    import in a process)."""
    lb_mod._litellm_ready = True
    fake_module = _FakeLitellmModule()
    call_count = {"n": 0}

    def _counting_ensure_ready():
        call_count["n"] += 1
        return fake_module

    monkeypatch.setattr(lb_mod, "ensure_litellm_ready", _counting_ensure_ready)

    result = ensure_litellm_ready_or_defer()  # must not raise

    assert result is fake_module
    assert call_count["n"] == 1, (
        "the already-ready fast path must still call ensure_litellm_ready() "
        "once (cheap baseline-capture housekeeping)"
    )
    warm_threads = [t for t in threading.enumerate() if t.name == "reyn-litellm-warm"]
    assert warm_threads == [], "no worker thread should be spawned when litellm is already ready"
