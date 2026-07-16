"""Tier 2: estimate_tokens() must not misreport a valid ``count == 0`` litellm
result as a tokenizer failure.

THE BUG (#2961): ``litellm.token_counter(model=..., text="")`` returns ``0``
— it does NOT raise. ``engine.py``'s old guard, ``if count and count > 0:``,
treats a falsy ``0`` the same as a raised exception, so estimating an empty
string alone was enough to fire the ``token_counter_fallback`` warning even
though the tokenizer never failed. The warning's wording compounded this:
"falling back to chars//4 for this process" implied a permanent, process-wide
switch, but ``_token_counter_fallback_warned`` is only a warn-once log latch
— every subsequent ``estimate_tokens()`` call still retries
``litellm.token_counter`` normally. A reviewer who saw the warning fire
concluded (wrongly) that their entire measurement session had silently
degraded to chars//4, and retracted correct BPE-based findings as a result.

THE FIX: only a raised exception from ``litellm.token_counter`` (the
`except Exception` branch, unchanged) reaches the chars//4 fallback path now;
a returned ``count >= 0`` is treated as success and cached directly, and the
warning text no longer claims a process-wide fallback.

These tests exercise the REAL litellm.token_counter (no ``unittest.mock``) —
this environment's litellm is live and offline-capable via its local
tokenizer, per testing.ja.md's Mock-vs-Fake rule.
"""
from __future__ import annotations

import logging

import litellm
import pytest

from reyn.services.compaction import engine as engine_mod
from reyn.services.compaction.engine import estimate_tokens


@pytest.fixture(autouse=True)
def _clean_token_state():
    """Tier 2 hygiene: both the token-estimate cache and the warn-once latch
    are module-globals shared across the whole test process. Reset them for
    isolation (setup/teardown, not an assertion — testing.ja.md's Tier-4 ban
    is on *asserting* private state, not on resetting it between tests; the
    same pattern used by test_compaction_token_cache_incremental.py)."""
    engine_mod._token_cache.clear()
    engine_mod._token_counter_fallback_warned = False
    yield
    engine_mod._token_cache.clear()
    engine_mod._token_counter_fallback_warned = False


def test_empty_string_estimate_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 2: estimating an empty string must NOT fire token_counter_fallback.

    Pins the bug's exact trigger: litellm.token_counter("") returns 0 (a
    valid answer), not an exception, so it must not be treated as a failure.
    """
    with caplog.at_level(logging.WARNING, logger=engine_mod.logger.name):
        estimate_tokens("", "gpt-4o")

    fallback_warnings = [
        r for r in caplog.records if "token_counter" in r.getMessage()
    ]
    assert fallback_warnings == [], (
        f"estimate_tokens('') must not warn about a token_counter failure "
        f"(count==0 is a valid result, not a failure); got: {fallback_warnings}"
    )


def test_empty_string_estimate_matches_real_litellm_count() -> None:
    """Tier 2: estimate_tokens("") returns litellm's real answer, not the
    chars//4 fallback's forced minimum of 1.

    Independent oracle: call litellm.token_counter directly and compare —
    this is exactly the same call estimate_tokens() makes internally, so a
    match here proves the BPE path (not the fallback path) produced the
    result.
    """
    expected = litellm.token_counter(model="gpt-4o", text="")
    result = estimate_tokens("", "gpt-4o")
    assert result == expected, (
        f"estimate_tokens('') should equal litellm.token_counter's own "
        f"answer ({expected}), not silently swap in the chars//4 fallback "
        f"value; got {result}"
    )
    # Document the currently-measured real value so a future litellm/tokenizer
    # change that flips this away from 0 is visible rather than silently
    # accepted (the chars//4 fallback's forced max(1, ...) would have made
    # this 1; the real tokenizer's answer for an empty string is 0).
    assert result == 0


def test_nonempty_estimate_still_succeeds_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: a normal non-empty estimate is unaffected by the fix — still
    uses the real tokenizer, still no spurious warning.
    """
    with caplog.at_level(logging.WARNING, logger=engine_mod.logger.name):
        result = estimate_tokens("hello world", "gpt-4o")

    assert result > 0
    fallback_warnings = [
        r for r in caplog.records if "token_counter" in r.getMessage()
    ]
    assert fallback_warnings == []
