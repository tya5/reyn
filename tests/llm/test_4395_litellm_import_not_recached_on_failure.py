"""Tier 2: `ensure_litellm_ready()` returns the imported litellm module
(or `None` on failure), a failure is NOT cached — the next call gets a
genuinely fresh attempt — and a failure is warned exactly once, not on
every call (#4395 PR-1).

THE BUG: a live owner repro (py-spy stack) caught the event-loop/UI
thread blocked inside `import litellm`. The chokepoint's own docstring
claimed a caller's subsequent bare `import litellm` was "cheap — Python
caches the module" — true only on SUCCESS. A FAILED import is NOT
cached by Python (a module whose top-level code raises is evicted from
`sys.modules`), so `model_budget.py` / `model_cost_rate.py` / `llm.py` /
`litellm_provider.py` / 3 sites in `pricing.py` each doing their OWN
redundant bare `import litellm` right after calling this chokepoint were
independently re-attempting — and re-failing — the exact same slow,
unbounded import, turning one attempt into two per call.

Uses `builtins.__import__` patching to simulate a genuine import
failure — the same class of technique already used elsewhere in this
session's own #4399 falsify tests (blocking real sockets to prove a
network attempt was genuinely made/prevented) — not a mock of any reyn
object, an interception of Python's own import mechanism to control a
third-party package's outcome deterministically.

#4395 axis②/PR-2 added a cooldown between repeated failed attempts (a
gap this PR-1 file's own tests originally missed — a live owner repro
caught the process still re-attempting, and re-hanging on, the same
broken TLS handshake on every subsequent call after PR-1 alone landed).
This file's own autouse fixture resets `_litellm_import_cooldown_until`
to 0.0 before every test so PR-1's tests keep exercising "not
PERMANENTLY cached" without being rate-limited by that cooldown; the
cooldown's own rate-limiting behavior is covered by the dedicated
cooldown test file instead, not duplicated here.
"""
from __future__ import annotations

import builtins
import logging

import pytest

import reyn.llm.litellm_bootstrap as lb_mod
from reyn.llm.litellm_bootstrap import ensure_litellm_ready


@pytest.fixture(autouse=True)
def _clean_litellm_bootstrap_state():
    """Tier 2 hygiene: this module's readiness state is process-global —
    reset the ownership registry and the warn-once latch between tests so
    one test's failure doesn't leave the NEXT test seeing a stale
    "already warned" or "already owned" state.

    `_litellm_ready` is ALSO forced False for the duration of every test in
    this file, restored after. In production, "ready" is real, permanent
    state (a success is never re-attempted) — but that fast path
    (`if _litellm_ready: return sys.modules.get("litellm")`) bypasses the
    `__import__` patch these tests rely on entirely, so any test that ran
    AFTER a real, successful litellm import elsewhere in the same pytest
    session would see the fast path short-circuit before ever reaching the
    patched import machinery — a test-isolation bug (a prior process-wide
    success leaking into a test that means to force a fresh failure), not
    a change to what "ready" means in production.
    """
    original_ready = lb_mod._litellm_ready
    original_cooldown_until = lb_mod._litellm_import_cooldown_until
    lb_mod._litellm_ready = False
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = 0.0
    yield
    lb_mod._litellm_ready = original_ready
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = original_cooldown_until


@pytest.fixture()
def _force_litellm_import_failure(monkeypatch):
    """Makes every `import litellm` statement in the process raise, and
    reports how many times it was actually attempted — a REAL
    interception of Python's own import mechanism (not a mock of
    reyn's own object model), so `ensure_litellm_ready()`'s real code
    path (its own inner `import litellm`) is genuinely exercised."""
    real_import = builtins.__import__
    attempts = {"n": 0}

    def _failing_import(name, *args, **kwargs):
        if name == "litellm":
            attempts["n"] += 1
            raise RuntimeError("simulated persistent litellm import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _failing_import)
    return attempts


def test_a_failed_import_returns_none_not_raise(_force_litellm_import_failure):
    """Tier 2: `ensure_litellm_ready()` never raises on its own failure —
    matches its own documented contract (callers decide what to do with
    `None`, e.g. a fallback or their own explicit error)."""
    result = ensure_litellm_ready()
    assert result is None


def test_a_failed_import_is_not_permanently_cached_past_its_cooldown(
    _force_litellm_import_failure,
):
    """Tier 2: THE core defect this PR-1 fixes at the chokepoint level —
    a failure must not be permanently remembered as "done" the way a
    success is; the caller with no fallback (a real completion) needs to
    keep trying eventually, not be locked out forever after one
    environmental blip. #4395 axis②/PR-2 added a COOLDOWN between
    attempts (see the dedicated cooldown test file) — a call made WHILE
    still in cooldown does NOT retry (that is now the correct,
    intentional behavior, the opposite of this test's own pre-PR-2
    premise); this test simulates the cooldown having already elapsed
    (a direct, real manipulation of the module's own deadline variable,
    not a sleep) to isolate "not cached past its cooldown" from "rate-
    limited during its cooldown", which the dedicated cooldown test file
    covers separately."""
    ensure_litellm_ready()
    after_first = _force_litellm_import_failure["n"]
    lb_mod._litellm_import_cooldown_until = 0.0  # simulate: cooldown has elapsed
    ensure_litellm_ready()
    after_second = _force_litellm_import_failure["n"]
    assert after_second > after_first, (
        "a call made after the cooldown has elapsed must trigger a "
        "genuinely fresh import attempt, not reuse a cached failure result"
    )


def test_a_failure_is_warned_exactly_once_across_repeated_calls(
    _force_litellm_import_failure, caplog,
):
    """Tier 2: landing condition — a permanently-failing environment must
    be VISIBLE (not silently degrade every fallback with zero signal),
    but warned once, not on every call (#3368's own "warn every time
    punishes the disciplined caller" trap)."""
    with caplog.at_level(logging.WARNING, logger=lb_mod.__name__):
        for _ in range(5):
            ensure_litellm_ready()
    warning_messages = [
        r.getMessage() for r in caplog.records if "import litellm failed" in r.getMessage()
    ]
    # A single warn-once notice, not one per failing call — asserted by
    # unpacking into exactly one binding (canonical idiom: a wrong COUNT
    # fails the unpack itself, not a len(...) == N format pin).
    (single_warning,) = warning_messages
    assert "import litellm failed" in single_warning


def test_a_success_after_a_failure_is_returned_and_cached(monkeypatch):
    """Tier 2: recovery — once litellm genuinely imports (a proxy came
    back up, in the real scenario), the result is the real module and
    stays cached (a real success is permanent — `sys.modules` itself
    never re-runs a successfully imported module)."""
    real_import = builtins.__import__
    state = {"fail": True, "attempts": 0}

    def _flaky_import(name, *args, **kwargs):
        if name == "litellm":
            state["attempts"] += 1
            if state["fail"]:
                raise RuntimeError("simulated transient failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _flaky_import)

    assert ensure_litellm_ready() is None
    state["fail"] = False
    # #4395 axis②/PR-2: the first failure above armed a cooldown — clear it
    # (a direct, real manipulation of the module's own deadline variable,
    # not a sleep) so this recovery attempt is not itself rate-limited;
    # the cooldown mechanism's own behavior is covered by the dedicated
    # cooldown test file, this test is specifically about "a SUCCESS,
    # once attempted, stays cached."
    lb_mod._litellm_import_cooldown_until = 0.0
    result = ensure_litellm_ready()
    assert result is not None
    assert result.__name__ == "litellm"

    attempts_after_success = state["attempts"]
    ensure_litellm_ready()
    assert state["attempts"] == attempts_after_success, (
        "a successful import must be cached — a later call must not "
        "attempt the import again"
    )
