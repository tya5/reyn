"""Tier 1: shared mutable module-level state behind a lazy-init / check-then-
set idiom must be thread-safe (#3671 P1).

This is a PREREQUISITE for a planned future startup-warming thread (not added
in this PR — see #3671's P2/P3): today nothing but the main thread ever calls
these functions, so the race is latent, not live. Each test below drives the
REAL function (never a mock) with concurrent threads and asserts on an
OBSERVABLE consequence of the race (an exception, a stale value, a
partially-initialized value) — never on private dict/lock internals.

Owner directive (via lead-coder, #3671): prefer a non-lock fix over adding a
lock wherever one exists — a lock is itself embug surface. Per item:

- ``_token_cache``: the race was "reading mutates the cache" (an LRU-recency
  bump on every hit). Removing that makes a read ONE ``dict.get()`` call,
  atomic under the GIL by construction — no lock, but a real behaviour
  change (LRU -> FIFO eviction), stated in ``compaction/engine.py``, not
  hidden.
- ``ensure_litellm_ready``: ownership + ``concurrent.futures.Future``, not a
  lock — see ``litellm_bootstrap.py`` for why (a lock held across an ~1.8s
  setup body is real deadlock surface if that body ever changed to
  re-enter; a Future the owner resolves only after finishing has no such
  risk, and closes the "ready meant started, not finished" bug lead-coder
  found by construction, which a lock alone did not).
- ``_get_httpx_exc_types``: was TWO globals checked via ONE of them — an
  invariant-split-across-two-variables bug, not an exclusion gap. Fixed by
  merging to ONE tuple assigned in one atomic STORE (same shape as
  ``_get_retryable_litellm_exceptions``, which was already correct).
- ``_get_retryable_litellm_exceptions`` / ``_token_counter_fallback_warned``:
  left unguarded — reasoned explicitly (not just "same shape, same fix")
  that double-execution here is provably benign (a redundant rebuild
  producing an equal value / a duplicate log line), never a wrong or
  partial value a caller could observe.
"""
from __future__ import annotations

import sys
import threading

import pytest


@pytest.fixture(autouse=True)
def _aggressive_thread_switching():
    """#3671: CPython's default switch interval (5ms) rarely lands a context
    switch inside a microsecond-scale critical section, so a real race won't
    reproduce reliably at default settings — not because it's absent, but
    because the observation window is too coarse. Forcing a much shorter
    interval makes the GIL yield far more often, turning a rare interleaving
    into a reliable one across the thread counts used below."""
    original = sys.getswitchinterval()
    sys.setswitchinterval(0.00001)
    try:
        yield
    finally:
        sys.setswitchinterval(original)


def test_token_cache_concurrent_get_put_does_not_raise(monkeypatch):
    """Tier 1: #3671 — the original ``_token_cache_get`` bumped LRU recency
    on every hit (``move_to_end``), so a read was check-then-act (``in`` ->
    ``move_to_end`` -> ``[]``), not one operation — a concurrent
    ``_token_cache_put``'s eviction could remove the key in between,
    raising ``KeyError``. Fixed by dropping recency tracking entirely (FIFO
    eviction instead of LRU, a stated behaviour change — see
    ``compaction/engine.py``): a read is now ONE ``dict.get()`` call, atomic
    under the GIL, with no separate step left to race. Witnessed directly
    (16 threads, 500 iterations, maxsize=4): reliably reproduces KeyError
    against the pre-fix (recency-bumping) shape, reliably clean after."""
    from reyn.services.compaction import engine as compaction_engine

    monkeypatch.setattr(compaction_engine, "_TOKEN_CACHE_MAXSIZE", 4)
    compaction_engine._token_cache.clear()

    errors: list[Exception] = []
    errors_lock = threading.Lock()
    n_threads = 16
    barrier = threading.Barrier(n_threads)

    def worker(n: int) -> None:
        barrier.wait()
        for i in range(500):
            key = ("model", f"text-{i % 8}")
            try:
                compaction_engine._token_cache_put(key, i)
                compaction_engine._token_cache_get(key)
            except Exception as exc:  # noqa: BLE001 — the race IS the assertion
                with errors_lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"race raised {len(errors)} exception(s); first: {errors[:1]!r}"


def test_token_cache_eviction_is_fifo_not_lru(monkeypatch):
    """Tier 1: #3671 — states the behaviour change plainly, as its own
    assertion, not just a docstring note: re-accessing a key no longer
    protects it from eviction. The OLDEST-inserted key is evicted first
    regardless of how recently it was read. Driven entirely through the
    PUBLIC ``estimate_tokens`` surface (never the private cache dict): a
    cache hit/miss is observed indirectly, via whether the underlying
    ``litellm.token_counter`` gets called again for the same input.

    #4395 PR-2: ``estimate_tokens`` now calls the NON-blocking
    ``ensure_litellm_ready_or_defer()`` — its real, intended behavior on
    the very FIRST call in a process with litellm not yet ready is to
    defer to the background warming thread and fall back immediately
    (never block), so this test's own real-world precondition (litellm
    already ready, matching every call after the process's first) is
    made explicit here rather than left to incidental test-order luck."""
    import litellm

    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    from reyn.services.compaction import engine as compaction_engine

    ensure_litellm_ready()  # real precondition: litellm already ready
    monkeypatch.setattr(compaction_engine, "_token_counter_cooldown_until", 0.0)
    monkeypatch.setattr(compaction_engine, "_TOKEN_CACHE_MAXSIZE", 2)
    compaction_engine._token_cache.clear()

    calls: list[str] = []

    def fake_token_counter(*, model, text):
        calls.append(text)
        return len(text)

    monkeypatch.setattr(litellm, "token_counter", fake_token_counter)

    compaction_engine.estimate_tokens("aaa", "some-model")
    compaction_engine.estimate_tokens("bbb", "some-model")
    # Repeatedly re-accessing "aaa" would have kept it MRU (protected) under
    # the old LRU policy. Under FIFO it has no effect on eviction order.
    for _ in range(5):
        compaction_engine.estimate_tokens("aaa", "some-model")
    compaction_engine.estimate_tokens("ccc", "some-model")  # over maxsize=2

    calls.clear()
    compaction_engine.estimate_tokens("aaa", "some-model")
    assert calls == ["aaa"], (
        "expected a cache MISS (litellm.token_counter recomputed) for the "
        "oldest-inserted key despite its recent re-access — FIFO, not LRU"
    )


def test_ensure_litellm_ready_flag_means_finished_not_merely_started(monkeypatch):
    """Tier 1: #3671 — the REAL defect here (found by lead-coder, heavier
    than a missing lock) is that ``_litellm_ready`` used to flip to ``True``
    BEFORE the ~1.8s ``import litellm`` + ``litellm.aiohttp_trust_env = True``
    setup completed, not after. A concurrent caller arriving during that
    window would see the flag already ``True`` and return immediately,
    believing setup done while ``aiohttp_trust_env`` was still ``False`` —
    the flag meant "someone started", not "finished". #3075 names this the
    sole chokepoint applying that flip for every LLM/embedding egress call,
    so a caller proceeding early is a real behavioural gap (a request made
    in that window would be proxy-blind), not just redundant work.

    Fixed with ownership + a ``concurrent.futures.Future`` (not a lock, per
    owner directive) — see ``litellm_bootstrap.py``. No narrow-window
    forcing needed: this widens the EXISTING ~1.8s window with a stand-in
    sleep in the same spot the real import costs it, so the test runs in
    milliseconds instead of seconds. Thread A starts the real setup; the
    main thread (B) calls ``ensure_litellm_ready()`` shortly after and
    asserts ``litellm.aiohttp_trust_env`` is already ``True`` by the time
    B's own call returns — B must never observe "ready" while A is still
    working."""
    import contextlib
    import time

    from reyn.llm import litellm_bootstrap

    monkeypatch.setattr(litellm_bootstrap, "_litellm_ready", False)
    monkeypatch.setattr(litellm_bootstrap, "_ready_registry", {})
    real_cm = litellm_bootstrap._litellm_import_logs_to_file

    @contextlib.contextmanager
    def slow_cm():
        with real_cm():
            time.sleep(0.2)  # stand-in for the real ~1.8s litellm import
            yield

    monkeypatch.setattr(litellm_bootstrap, "_litellm_import_logs_to_file", slow_cm)

    import litellm

    monkeypatch.setattr(litellm, "aiohttp_trust_env", False, raising=False)

    def call_a() -> None:
        litellm_bootstrap.ensure_litellm_ready()

    thread_a = threading.Thread(target=call_a)
    thread_a.start()
    time.sleep(0.05)  # let A win ownership and get into the "slow work"
    litellm_bootstrap.ensure_litellm_ready()  # B — called on the main thread
    b_observed_trust_env = litellm.aiohttp_trust_env
    thread_a.join()

    assert b_observed_trust_env is True, (
        "B's ensure_litellm_ready() returned before A's setup finished — "
        "litellm.aiohttp_trust_env was still not True"
    )


def test_httpx_exc_types_consistent_under_concurrency(monkeypatch):
    """Tier 1: #3671 — ``_get_httpx_exc_types()`` used to be TWO globals
    checked via ONE of them directly, so a thread scheduled between the two
    assignment statements could observe a torn pair (one member still
    ``None``). Fixed by merging to ONE global holding the complete tuple,
    assigned in a single atomic STORE — structurally, there is no longer an
    intermediate state ANY reader could observe (the tuple is fully built
    in a local temporary before the assignment even happens), so this is
    now a correctness-under-concurrency check like
    ``_get_retryable_litellm_exceptions`` below, not a torn-value witness."""
    from reyn.llm import llm as llm_module

    monkeypatch.setattr(llm_module, "_HTTPX_EXC_TYPES", None)

    results: list[tuple] = []
    results_lock = threading.Lock()

    def worker() -> None:
        r = llm_module._get_httpx_exc_types()
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    import httpx

    assert len(results) == len(threads), "a worker never returned"
    assert all(
        r == (httpx.ConnectError, httpx.ReadTimeout) for r in results
    ), f"a concurrent caller observed a wrong/partial pair: {results}"


def test_retryable_litellm_exceptions_consistent_under_concurrency(monkeypatch):
    """Tier 1: #3671 — ``_get_retryable_litellm_exceptions()``'s check-then-set
    on ``_RETRYABLE_LITELLM_EXCEPTIONS`` is left unguarded (owner directive:
    no lock without a real correctness need). Reasoned explicitly, not just
    "same shape as the others": the checked global and the assigned global
    are the SAME variable, in one atomic STORE, so a reader only ever
    observes ``None`` or the complete tuple — a concurrent racer can
    redundantly rebuild it (never a wrong or partial value).

    #4395 PR-2 (architect measurement): this function no longer imports
    litellm on its own AT ALL — it only reads `sys.modules["litellm"]`
    once `is_litellm_ready()` confirms it is already there (a second,
    independent `import litellm` from this function would contend on
    CPython's own per-module import lock with the dedicated background
    warming thread's own in-flight import, blocking main for the exact
    duration that thread exists to avoid). This test's own real-world
    precondition — this function is only ever reached AFTER a real
    completion attempt already ran (and so already imported litellm) —
    is made explicit here rather than left to incidental test-order luck:
    litellm is confirmed ready BEFORE the concurrent workers start."""
    from reyn.llm import llm as llm_module
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    ensure_litellm_ready()  # real precondition: litellm already ready
    monkeypatch.setattr(llm_module, "_RETRYABLE_LITELLM_EXCEPTIONS", None)

    results: list[tuple] = []
    results_lock = threading.Lock()

    def worker() -> None:
        r = llm_module._get_retryable_litellm_exceptions()
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    import litellm

    expected = {
        litellm.exceptions.Timeout,
        litellm.exceptions.APIConnectionError,
        litellm.exceptions.ServiceUnavailableError,
        litellm.exceptions.BadGatewayError,
        litellm.exceptions.InternalServerError,
    }
    assert len(results) == len(threads), "a worker never returned"
    assert all(r is not None and set(r) == expected for r in results), (
        f"a concurrent caller observed an incomplete/wrong exception set: {results}"
    )
