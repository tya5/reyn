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

#6073 follow-up (2026-09-10 CI failure): a 4th real overflow shape —
``"context_length_exceeded"`` (litellm's/OpenAI's own ``error.code``
spelling, confirmed in the installed ``litellm`` package source, arriving
here as free text inside a plain exception's message rather than as the
already-handled structured ``.code`` attribute) — was added to
``_CONTEXT_OVERFLOW_KEYWORDS``. It is underscore-joined, not
whitespace-joined, so the structural gate below was widened from "must
contain whitespace" to "must contain whitespace OR an underscore" (see
``_is_multi_component``'s own docstring) rather than silently adding a
keyword the gate itself would otherwise have rejected.
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
    logic, to witness the literal needle strings this PR did not touch.

    #6073 co-vet (architect's finding): source-pinning means a FUTURE
    follow-up that converts these 5 sites from a ``"HTTP 404" in
    str(exc)`` string match to a real status-code check (each site's own
    exception — ``RegistryError``/``litellm``'s own error types — carries
    no structured status_code of its own today; adding one is a DIFFERENT
    PR's scope, not this one's) will make THIS test go red.

    Deliberately kept as a source-pin rather than converted to a
    behavioural check (option (a) the co-vet also offered): these 5 sites
    are NOT one shared classifier predicate the way
    ``is_context_overflow_error`` is — they are 3 different modules
    (``mcp_install.py``, ``registry/client.py``, ``mcp/registry.py``, plus
    ``router_loop.py``'s 2 string checks) each doing DIFFERENT things with
    the match (continue-to-next-URL, raise-with-guidance, emit a
    parameter-stripping retry) — there is no single function to call and
    assert True/False against; building a behavioural harness per site
    would mean faking each site's own I/O boundary (an HTTP client, a
    litellm call) for a test whose only job is "did the 5 sites move",
    which is a materially larger test than the question being asked.

    THIS RED IS EXPECTED AND CORRECT, not a regression: when that
    follow-up lands, delete or rewrite this test in THAT PR — do not
    resurrect the old string literals to keep it green."""
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
    matched or on what exception type).

    #6073 co-vet: this is ``logger.warning``, not ``logger.info`` —
    ``chat.py``'s shipped default config sets the ROOT logger to
    ``WARNING``, so an ``info`` record here would be silently discarded
    in every shipped run (the same trap #6045 hit the same day). Asserted
    at ``logging.WARNING`` here, not ``INFO``, so this test cannot stay
    green against a regression back to ``info``."""
    exc = litellm.BadRequestError(
        message="the input is too large for this model",
        model="gpt-4", llm_provider="openai",
    )
    with caplog.at_level(logging.WARNING, logger="reyn.services.compaction.engine"):
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
    assert all(r.levelno >= logging.WARNING for r in matched_records), (
        "the deciding (True) branch must be visible at the shipped "
        "default root level (WARNING) -- not silently below it"
    )


def test_fallback_no_match_is_also_reported_not_silent(caplog) -> None:
    """Tier 2: acceptance 5, the False-deciding half — a message that
    reaches the fallback but matches nothing is also reported (not only
    the True case), so a future false negative is diagnosable via the
    same surface.

    #6073 co-vet: this half stays at ``logger.debug``, deliberately NOT
    raised to ``warning`` alongside the True branch above — it fires on
    EVERY exception that reaches this fallback and matches nothing
    (every RETRYABLE/FATAL exception lacking a typed/status/code signal
    funnels through here too), so raising it to ``warning`` would bury
    the one warning that actually matters in noise. Asserted at
    ``logging.DEBUG`` here (not ``WARNING``), matching what the
    production code actually emits."""
    exc = MissingFixture("... safety_limit_no_listener.jsonl ...")
    with caplog.at_level(logging.DEBUG, logger="reyn.services.compaction.engine"):
        assert is_context_overflow_error(exc) is False
    assert any(
        "MissingFixture" in r.getMessage() and "False" in r.getMessage()
        for r in caplog.records
    ), "the no-match decision must also be reported, not silent"


# ---------------------------------------------------------------------------
# Structural gate: _CONTEXT_OVERFLOW_KEYWORDS may contain no single-word
# (whitespace-free) element (acceptance 6, gate half)
# ---------------------------------------------------------------------------


def _is_multi_component(kw: str) -> bool:
    """Shared by the gate and its falsify-test below: *kw* is either a
    whitespace-joined PHRASE or an underscore-joined error-CODE-shaped
    identifier — never a single bare word.

    #6073 (2026-09-10 CI failure, the gate's own first real collision):
    ``"context_length_exceeded"`` is litellm's/OpenAI's own ``error.code``
    spelling (confirmed verbatim in the installed ``litellm`` package —
    ``litellm/types/llms/openai.py``), joined by underscores rather than
    spaces because that is how the PROVIDER spells its own error code —
    this module has no authority to re-punctuate a third party's literal
    identifier into a space-joined phrase just to satisfy this gate's
    original whitespace check. The architect's own discriminator (#6069,
    2nd comment) never said "must contain whitespace" — it said "a
    partial match is safe only when the spelling can ONLY appear when
    the condition is true"; whitespace was this gate's PROXY for that
    principle, built when every known phrase happened to be space-joined.
    An underscore-joined multi-component identifier is exactly as narrow
    a spelling as a space-joined phrase (arguably narrower: it is a
    known, fixed provider error-code string, not assembled English) — so
    the proxy is widened to "contains whitespace OR an underscore",
    rather than silently bypassed by adding the new keyword without
    updating the check it must still pass. A single bare word like
    ``"limit"`` contains neither and still fails both forms of the gate."""
    return any(ch.isspace() for ch in kw) or "_" in kw


def test_context_overflow_keywords_contain_no_bare_words() -> None:
    """Tier 1: the structural gate itself — every element of
    ``_CONTEXT_OVERFLOW_KEYWORDS`` must be multi-component (a whitespace-
    joined PHRASE, or an underscore-joined error-code-shaped identifier —
    see :func:`_is_multi_component`'s own docstring for why underscore
    joins this gate's "not a bare word" check rather than bypassing it),
    scoped to ONLY this one constant, never a general "ban short
    keywords" rule over the codebase.

    Deliberately STRUCTURAL (checks each string's own punctuation), not a
    hand-maintained list of "banned words" — the point is that nobody can
    re-add a bare single word to THIS constant, including a word nobody
    has thought of yet, without this failing.

    Disclosed limit (PR body's own caveat, restated here): passing this
    check does not mean collision-IMPOSSIBLE — a phrase like "too large"
    could still theoretically appear in an unrelated message (the
    architect's own example: a filename containing "context window").
    This gate raises the bar the discriminator sets; it does not claim
    to make a false positive impossible."""
    for kw in _CONTEXT_OVERFLOW_KEYWORDS:
        assert _is_multi_component(kw), (
            f"{kw!r} is a single bare word (no whitespace, no "
            "underscore) — _CONTEXT_OVERFLOW_KEYWORDS may only contain "
            "multi-word phrases or underscore-joined error-code-shaped "
            "identifiers (see this test's own docstring and the #6069 "
            "PR body)"
        )


def test_context_overflow_keywords_gate_is_falsified_by_restoring_a_bare_word() -> None:
    """Tier 1: strip-falsify (acceptance 6, gate half) — a LOCAL tuple
    with a general word put back in must fail the SAME structural check
    the test above runs against production, confirming the gate is
    sensitive to exactly this shape and not vacuously green regardless
    of content. Never mutates the production constant."""
    reverted = (*_CONTEXT_OVERFLOW_KEYWORDS, "limit")
    assert not all(_is_multi_component(kw) for kw in reverted), (
        "restoring a bare single word must make the structural check go "
        "red — if it doesn't, the check isn't testing what it claims to"
    )
