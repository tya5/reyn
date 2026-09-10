"""Tier 2: #6069 — ``_CONTEXT_OVERFLOW_KEYWORDS`` must not partial-match on
a general, common-English word (a spelling that can appear in a message for
a reason that has NOTHING to do with overflow) — only on a multi-word
PHRASE (a materially narrower, though not impossible-to-collide, spelling).

Real false positive this issue started from: a CI run's own
``reyn.dev.testing.replay.MissingFixture`` (a test-harness "no recorded
fixture matched this call" error, never a provider overflow) carried a
fixture FILENAME containing the substring "limit" —
``safety_limit_no_listener.jsonl`` — and matched
``is_shrinkable_overflow``'s own keyword fallback purely by coincidence
(#6070). The architect's discriminator (issue #6069, 2nd comment, after
withdrawing a first "provenance" cut that conflated WHO wrote a message
with WHERE its content came from): a partial match is safe only when the
spelling can ONLY appear when the condition is true. ``context``/``token``/
``length``/``limit`` fail that test; the phrases (``too long``/``too
large``) pass it — narrower, not impossible (a PR body's own disclosure:
a filename containing "context window" could still collide).

Four things pinned here:
(1) the exact false positive this issue started from → False (acceptance 1)
(2) a present-sibling positive control (a flattened 3rd-party error with no
    typed signal, message carrying a surviving phrase) → True — rules out
    an over-correction that would make this predicate always False
    (acceptance 2)
(3) the out-of-scope family sites (router_loop.py's 3 "HTTP 404" sites +
    "does not support parameter" + "encoding_format") are provably
    untouched — a witness this PR did not sweep the whole family under one
    name (acceptance 3)
(4) the structural gate on ``_CONTEXT_OVERFLOW_KEYWORDS`` itself, plus its
    own strip-falsify (acceptance 6)

The fallback-reporting acceptance item (5) and the "2 typed signals not
vacuous" item (4 in the issue's own numbering) are pinned in
``tests/runtime/test_router_loop_pure_helpers.py`` and
``tests/core/test_4381_stage1_overflow_classification.py`` respectively
(the 413/litellm-type checks, already-existing and already covered there);
see this file's own last test for the fallback-reporting witness anyway,
via the public ``caplog`` surface (never a private field).
"""
from __future__ import annotations

import logging

import litellm

from reyn.dev.testing.replay import MissingFixture
from reyn.services.compaction.engine import (
    _CONTEXT_OVERFLOW_KEYWORDS,
    is_context_overflow_error,
    is_shrinkable_overflow,
)


def test_missing_fixture_filename_collision_is_no_longer_overflow() -> None:
    """Tier 2: acceptance 1 — the exact defect #6069/#6070 started from.

    A ``MissingFixture`` whose message carries a fixture FILENAME
    containing the substring "limit" (a harness detail with nothing to do
    with overflow) must not classify as context-overflow, and must not
    enter the shrink ladder."""
    exc = MissingFixture(
        "no fixture entry matches this call "
        "(tests/_fixtures/llm/safety_limit_no_listener.jsonl)"
    )
    assert is_context_overflow_error(exc) is False
    assert is_shrinkable_overflow(exc) is False


def test_present_sibling_flattened_third_party_phrase_is_still_overflow() -> None:
    """Tier 2: acceptance 2 — present sibling. A real, flattened
    ``litellm.BadRequestError`` (no ``status_code``/type/structured
    ``code`` signal — the proxy-flattening shape this fallback exists
    for) whose free-text message carries a surviving PHRASE still
    classifies as overflow. Without this test, acceptance 1 above could
    pass trivially with an implementation that always returns False."""
    exc = litellm.BadRequestError(
        message="The request is too large for this model's context window.",
        model="gpt-4", llm_provider="openai",
    )
    assert getattr(exc, "status_code", None) != 413
    assert getattr(exc, "code", None) is None
    assert is_context_overflow_error(exc) is True


def test_strip_falsify_restoring_a_general_word_reclassifies_the_fixture_true() -> None:
    """Tier 2: strip-falsify (acceptance 6, classification half) — putting
    "limit" back into a LOCAL copy of the keyword set (never mutating the
    production constant itself — this module-level tuple is shared, and
    tests must not mutate shared production state) reproduces the
    original false positive, confirming this test's own positive-side
    assertion is not vacuously green regardless of the word list."""
    exc = MissingFixture("... safety_limit_no_listener.jsonl ...")
    message = str(exc).lower()
    assert not any(kw in message for kw in _CONTEXT_OVERFLOW_KEYWORDS), (
        "sanity: the CURRENT (post-#6069) keyword set must not match this "
        "message at all"
    )
    reverted_keywords = (*_CONTEXT_OVERFLOW_KEYWORDS, "limit")
    assert any(kw in message for kw in reverted_keywords), (
        "restoring the removed general word 'limit' must reproduce the "
        "original false positive on this exact message — confirming "
        "'limit' (not something else) was what matched before #6069"
    )


def test_out_of_scope_http_404_sites_are_unchanged() -> None:
    """Tier 2: acceptance 3 — out-of-scope witness. The architect's own
    ruling excluded the other 5 family sites from this fix (they pass the
    same discriminator, or are not partial-string-matches at all) — reads
    each site's own source text directly, never re-implementing its
    logic, to witness the literal needle strings this PR did not touch."""
    import inspect

    from reyn.core.op_runtime import mcp_install
    from reyn.core.registry import client as registry_client
    from reyn.mcp import registry as mcp_registry
    from reyn.runtime import router_loop

    assert '"HTTP 404" in str(exc)' in inspect.getsource(mcp_install)
    assert '"HTTP 404" not in str(exc)' in inspect.getsource(registry_client)
    assert '"HTTP 404" in str(exc)' in inspect.getsource(mcp_registry)
    router_loop_src = inspect.getsource(router_loop)
    assert '"does not support parameter" in str(exc)' in router_loop_src
    assert '"encoding_format" in str(exc)' in router_loop_src


def test_fallback_match_is_reported_not_silent(caplog) -> None:
    """Tier 2: acceptance 5 — when the keyword fallback is what DECIDES a
    classification (no typed/status_code/structured-code signal reached
    first), the matched spelling and the exception's type name are now
    reported via the public logging surface — never silent, the way the
    original #6070 false positive was (nothing recorded WHICH spelling
    matched or on what exception type)."""
    exc = litellm.BadRequestError(
        message="the input is too large for this model",
        model="gpt-4", llm_provider="openai",
    )
    with caplog.at_level(logging.INFO, logger="reyn.services.compaction.engine"):
        assert is_context_overflow_error(exc) is True
    matched_records = [r for r in caplog.records if "too large" in r.getMessage()]
    assert matched_records, (
        "the matched keyword ('too large') must appear in a log record, "
        "not be silently decided"
    )
    assert any("BadRequestError" in r.getMessage() for r in matched_records), (
        "the exception's type name must also be reported alongside the "
        "matched spelling"
    )


def test_fallback_no_match_is_also_reported_not_silent(caplog) -> None:
    """Tier 2: acceptance 5, the False-deciding half — a message that
    reaches the fallback but matches nothing must also be reported (not
    only the True case), so a future FALSE NEGATIVE (a real overflow
    message missing every remaining phrase — see
    ``tests/runtime/test_5699_compaction_window_fold_parity.py``'s own
    #6069 positive-control test) is diagnosable via the same surface."""
    exc = MissingFixture("... safety_limit_no_listener.jsonl ...")
    with caplog.at_level(logging.INFO, logger="reyn.services.compaction.engine"):
        assert is_context_overflow_error(exc) is False
    assert any(
        "MissingFixture" in r.getMessage() and "False" in r.getMessage()
        for r in caplog.records
    ), "the no-match decision must also be reported, not silent"


# ---------------------------------------------------------------------------
# Structural gate: _CONTEXT_OVERFLOW_KEYWORDS may contain no single-word
# (whitespace-free) element (acceptance 6, gate half)
# ---------------------------------------------------------------------------


def test_context_overflow_keywords_contain_no_bare_words() -> None:
    """Tier 1: the structural gate itself — every element of
    ``_CONTEXT_OVERFLOW_KEYWORDS`` must contain whitespace (i.e. be a
    multi-word PHRASE), scoped to ONLY this one constant, never a
    general "ban short keywords" rule over the codebase.

    Deliberately STRUCTURAL (checks whether each string contains
    whitespace), not a hand-maintained list of "banned words" — the
    point is that nobody can re-add a bare single word to THIS constant,
    including a word nobody has thought of yet, without this failing.

    Disclosed limit (PR body's own caveat, restated here): whitespace-
    containing does not mean collision-IMPOSSIBLE — a phrase like "too
    large" could still theoretically appear in an unrelated message (the
    architect's own example: a filename containing "context window").
    This gate raises the bar the discriminator sets; it does not claim
    to make a false positive impossible."""
    for kw in _CONTEXT_OVERFLOW_KEYWORDS:
        assert any(ch.isspace() for ch in kw), (
            f"{kw!r} is a single word (no whitespace) — "
            "_CONTEXT_OVERFLOW_KEYWORDS may only contain multi-word "
            "phrases (see this test's own docstring and the #6069 PR body)"
        )


def test_context_overflow_keywords_gate_is_falsified_by_restoring_a_bare_word() -> None:
    """Tier 1: strip-falsify (acceptance 6, gate half) — a LOCAL tuple
    with a general word put back in must fail the SAME structural check
    the test above runs against production, confirming the gate is
    sensitive to exactly this shape and not vacuously green regardless
    of content. Never mutates the production constant."""
    reverted = (*_CONTEXT_OVERFLOW_KEYWORDS, "limit")
    assert not all(any(ch.isspace() for ch in kw) for kw in reverted), (
        "restoring a bare single word must make the structural check go "
        "red — if it doesn't, the check isn't testing what it claims to"
    )
