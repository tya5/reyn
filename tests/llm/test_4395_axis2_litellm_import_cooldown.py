"""Tier 2: a failed `import litellm` starts a cooldown window — a call
made WHILE still inside it returns `None` immediately without attempting
a fresh import; a call made AFTER it elapses does attempt again (#4395
axis②/PR-2).

THE GAP: PR-1 (#4413) closed the WITHIN-one-call double attempt (a
caller's own redundant bare `import litellm` right after the chokepoint)
but deliberately did NOT cache a failure — by design, the NEXT call
starts a genuinely fresh attempt. A live owner repro (py-spy stack, AFTER
PR-1 had already landed) caught the process still blocked inside a TLS
handshake at `litellm_bootstrap.py`'s own internal `import litellm` line
— PR-1 removed the duplicate attempt WITHIN one call, but a persistently
broken environment (the owner's own case: a handshake that never
completes) was still being re-attempted, and re-hung-on, on EVERY
subsequent call, not just once. This file covers the fix: a failure now
starts a cooldown (`reyn._cooldown`, the same primitive #4398 already
established for the identical shape in `compaction/engine.py`'s
`estimate_tokens`) during which every call returns `None` immediately.

Uses `builtins.__import__` patching (not `unittest.mock`) — same
technique as `test_4395_litellm_import_not_recached_on_failure.py`.
"""
from __future__ import annotations

import builtins

import pytest

import reyn.llm.litellm_bootstrap as lb_mod
from reyn.llm.litellm_bootstrap import ensure_litellm_ready


@pytest.fixture(autouse=True)
def _clean_litellm_bootstrap_state():
    """Tier 2 hygiene — see the identical fixture in
    test_4395_litellm_import_not_recached_on_failure.py for the full
    "why force _litellm_ready False" reasoning; this file additionally
    resets the cooldown deadline so no test in this file starts inside a
    cooldown armed by an earlier test."""
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
    reports how many times it was actually attempted."""
    real_import = builtins.__import__
    attempts = {"n": 0}

    def _failing_import(name, *args, **kwargs):
        if name == "litellm":
            attempts["n"] += 1
            raise RuntimeError("simulated persistent litellm import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _failing_import)
    return attempts


def test_a_call_during_the_cooldown_returns_none_without_attempting_import(
    _force_litellm_import_failure,
):
    """Tier 2: THE core fix. A call made immediately after a failure (well
    within the default cooldown window) must not attempt `import litellm`
    again at all — the owner-observed defect was exactly this: every
    subsequent call re-attempting, and re-hanging on, the same broken
    network operation."""
    ensure_litellm_ready()
    after_first = _force_litellm_import_failure["n"]

    result = ensure_litellm_ready()  # still well within the cooldown window

    assert result is None
    assert _force_litellm_import_failure["n"] == after_first, (
        "a call during the cooldown must not attempt a fresh import"
    )


def test_a_call_after_the_cooldown_elapses_attempts_again(
    _force_litellm_import_failure,
):
    """Tier 2: the cooldown RATE-LIMITS retrying, it does not permanently
    give up — the underlying cause may clear (a proxy comes back), and
    there is no restart hook to notice that on its own, so the first call
    after the window elapses must re-probe. Simulates elapsed time via a
    direct, real manipulation of the module's own deadline variable (the
    thing the cooldown check actually reads), not a sleep — matches
    testing.md's ban on a straight-line `sleep(N)` to make an assertion
    pass."""
    ensure_litellm_ready()
    after_first = _force_litellm_import_failure["n"]

    lb_mod._litellm_import_cooldown_until = 0.0  # simulate: cooldown has elapsed
    ensure_litellm_ready()
    after_second = _force_litellm_import_failure["n"]

    assert after_second > after_first, (
        "a call after the cooldown has elapsed must attempt a fresh import"
    )


def test_the_cooldown_check_never_imports_litellm_itself(monkeypatch):
    """Tier 2: accept-side — the cooldown check itself is a cheap
    `time.monotonic()` comparison, not something that could itself touch
    litellm's import machinery (which would defeat the whole point)."""
    import reyn._cooldown as cooldown_mod

    calls = {"n": 0}
    real_in_cooldown = cooldown_mod.in_cooldown

    def _counting_in_cooldown(deadline):
        calls["n"] += 1
        return real_in_cooldown(deadline)

    monkeypatch.setattr(cooldown_mod, "in_cooldown", _counting_in_cooldown)
    monkeypatch.setattr(lb_mod, "_cooldown", cooldown_mod)

    real_import = builtins.__import__

    def _fail_only_on_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise AssertionError("import litellm must not be attempted while checking cooldown")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_only_on_litellm)
    lb_mod._litellm_import_cooldown_until = lb_mod._cooldown.new_cooldown_deadline(60.0)

    result = ensure_litellm_ready()

    assert result is None
    assert calls["n"] == 1
