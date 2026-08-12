"""Tier 2: a PERSISTENT litellm.token_counter failure enters a cooldown —
subsequent calls skip straight to chars//4 instead of paying the full
call+timeout again for every new text string (#4395).

THE BUG: estimate_tokens()'s cache is keyed by (model, text-hash) — a
one-off tokenizer failure retrying "as normal" on the NEXT call is correct
for a transient blip (network flap, rate limit), but a PERSISTENT,
environmental failure (SSL egress blocked, no proxy reachable) means every
DISTINCT text string is a fresh cache miss that pays the full failing call
again. estimate_tokens() runs synchronously from turn processing, so that
wait is paid on the UI thread each time (owner-reported: reyn's UI hangs;
consistent with — not proven caused by — this shape).

THE FIX: a failure starts a cooldown window
(``_TOKEN_COUNTER_COOLDOWN_SECONDS``); any call inside it skips
litellm.token_counter entirely (chars//4 immediately, no wait). A success —
whether the very next call after the cooldown expires, or any call once
litellm.token_counter is healthy again — clears the cooldown. This is
deliberately NOT a permanent give-up: the failure may be transient at a
longer timescale than one call (a proxy comes back up), and there is no
restart hook to notice that on its own.

Uses a REAL callable standing in for litellm.token_counter (not
unittest.mock), the same per-text-key call-count-dict technique
test_compaction_token_cache_incremental.py's own
``_counting_token_counter`` already established for this module — behavior
(was litellm.token_counter actually invoked for this text) is observed via
that dict, never by asserting the private cooldown deadline directly
(testing.ja.md Tier 4's private-state ban); the deadline is only ever
WRITTEN in these tests, as an input simulating "the cooldown has elapsed",
the same shape test_2961's own fixture already uses for this module's
other globals.
"""
from __future__ import annotations

import pytest

from reyn.services.compaction import engine as engine_mod
from reyn.services.compaction.engine import estimate_tokens


@pytest.fixture(autouse=True)
def _clean_token_state():
    """Tier 2 hygiene: reset all 3 module-globals this test file touches —
    same isolation reasoning as test_2961's own fixture."""
    engine_mod._token_cache.clear()
    engine_mod._token_counter_fallback_warned = False
    engine_mod._token_counter_cooldown_until = 0.0
    yield
    engine_mod._token_cache.clear()
    engine_mod._token_counter_fallback_warned = False
    engine_mod._token_counter_cooldown_until = 0.0


def _always_failing_token_counter(counts: dict):
    """Real callable (not a mock) standing in for a persistently broken
    litellm.token_counter — every call raises AND records itself in
    *counts*, keyed by the text it was asked to count (mirrors
    test_compaction_token_cache_incremental.py's own
    ``_counting_token_counter``) — so "was litellm.token_counter actually
    invoked for THIS text" is a public, observable fact, not a private-state
    read."""
    def _counter(*, model: str, text: str) -> int:
        counts[text] = counts.get(text, 0) + 1
        raise RuntimeError("simulated persistent SSL/network failure")
    return _counter


def test_persistent_failure_skips_the_next_call_entirely(monkeypatch) -> None:
    """Tier 2: #4395's actual defect. Without the cooldown, a SECOND call
    with DIFFERENT text (a fresh cache miss) would invoke
    litellm.token_counter again and pay the same failing wait — the whole
    point of the fix is that it doesn't."""
    counts: dict = {}
    monkeypatch.setattr(
        "litellm.token_counter", _always_failing_token_counter(counts),
    )

    text_a = "first distinct text"
    result_a = estimate_tokens(text_a, "test-model")
    assert counts.get(text_a) == 1, "the first call must genuinely attempt litellm.token_counter"
    assert result_a == max(1, len(text_a) // 4)

    text_b = "a completely different text string"
    result_b = estimate_tokens(text_b, "test-model")
    assert counts.get(text_b) is None, (
        "a second call with different text, while in cooldown, must NOT "
        "invoke litellm.token_counter at all — this is the defect #4395 fixes"
    )
    assert result_b == max(1, len(text_b) // 4)


def test_cooldown_expiry_re_probes_litellm_token_counter(monkeypatch) -> None:
    """Tier 2: the cooldown is temporary, not a permanent give-up — once it
    elapses, the next call tries litellm.token_counter again."""
    counts: dict = {}
    monkeypatch.setattr(
        "litellm.token_counter", _always_failing_token_counter(counts),
    )

    text_one = "text one"
    estimate_tokens(text_one, "test-model")
    assert counts.get(text_one) == 1

    # Simulate the cooldown having elapsed — deterministic, not a real sleep
    # (testing.md's own ban on straight-line sleep-to-make-it-pass): WRITE
    # the deadline into the past directly (an input, not an assertion — the
    # same simulate-state shape test_2961's own fixture already uses for
    # this module's globals), then observe the effect through behavior.
    engine_mod._token_counter_cooldown_until = 0.0

    text_two = "text two — still fails, but the cooldown had expired"
    estimate_tokens(text_two, "test-model")
    assert counts.get(text_two) == 1, (
        "once the cooldown window has elapsed, the next call must re-probe "
        "litellm.token_counter, not skip it forever"
    )


def test_a_success_clears_the_cooldown(monkeypatch) -> None:
    """Tier 2: recovery — once litellm.token_counter starts succeeding
    again, subsequent calls stop skipping it (no permanent cooldown latch)."""
    fail_counts: dict = {}
    monkeypatch.setattr(
        "litellm.token_counter", _always_failing_token_counter(fail_counts),
    )
    first_failing_text = "first failing text"
    estimate_tokens(first_failing_text, "test-model")
    assert fail_counts.get(first_failing_text) == 1

    # Now let the underlying call succeed (proxy came back up, in the real
    # scenario) and expire the cooldown so the next call actually re-probes
    # rather than being skipped by the still-active window from the failure
    # above.
    success_counts: dict = {}

    def _succeeding_token_counter(*, model: str, text: str) -> int:
        success_counts[text] = success_counts.get(text, 0) + 1
        return 42
    monkeypatch.setattr("litellm.token_counter", _succeeding_token_counter)
    engine_mod._token_counter_cooldown_until = 0.0

    success_text = "a distinct successful text"
    result = estimate_tokens(success_text, "test-model")
    assert result == 42
    assert success_counts.get(success_text) == 1

    # A THIRD call, still against the succeeding stand-in, must not be
    # skipped by a stale cooldown left over from the earlier failure — the
    # success above must have cleared it, observed here (not by reading the
    # deadline directly) via this call actually reaching the stand-in.
    another_success_text = "yet another distinct text"
    result2 = estimate_tokens(another_success_text, "test-model")
    assert result2 == 42
    assert success_counts.get(another_success_text) == 1
