"""Tier 2: #4381 stage 1 — classification and reporting fixes only, no
recovery-behaviour change (owner ruling: A/B are dependency-free and
independent of the layer/tool-contract design questions still awaiting
architect ruling for later stages).

A. is_context_overflow_error (services/compaction/engine.py) checks HTTP
413 via a real status_code attribute BEFORE falling back to the
"too large" keyword match — the same "type before string" principle the
function's own docstring already states for ContextWindowExceededError,
now applied to 413 too. Root cause of the original bug (owner's real-
environment observation): a 413 ("Request Entity Too Large" — a request-
BODY-byte limit, not the token-count limit the retry_loop's shrink logic
actually addresses) only matched via the loose keyword fallback, making
classification depend entirely on the exact wording of a provider's error
message.

B. Session._run_router_loop's top-level exception handler now surfaces
the DEEPEST __cause__ in the exception chain — both in the reyn.log line
and as a `cause` field on the router_loop_terminated_by_exception audit
event — so an operator sees what actually happened (e.g. APIError) rather
than only reyn's own wrapper type (ContextOverflowError), which is what
owner's real-environment reyn.log showed before this fix.
"""
from __future__ import annotations

from reyn.runtime.session import _deepest_cause
from reyn.services.compaction.engine import is_context_overflow_error


class _FakeStatusError(Exception):
    """A minimal stand-in for openai.APIStatusError's own shape (a plain
    `status_code` attribute set from the underlying HTTP response) —
    deliberately NOT a litellm/openai exception subclass, so this test
    exercises the ATTRIBUTE check on its own, independent of whichever
    litellm exception hierarchy happens to be installed."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_413_is_classified_via_status_code_with_no_overflow_wording() -> None:
    """Tier 2: #4381 A — a 413 error whose MESSAGE contains none of
    _CONTEXT_OVERFLOW_KEYWORDS is still classified as overflow, because
    status_code is checked before the keyword fallback ever runs. Proves
    the classification is no longer dependent on message wording at all."""
    # Deliberately worded to avoid every _CONTEXT_OVERFLOW_KEYWORDS entry
    # ("too large" included — a real 413 message often says exactly that,
    # which is precisely why the OLD code accidentally classified it
    # right; this test needs a message that could NOT have matched the
    # keyword fallback, to isolate the new type-based path).
    exc = _FakeStatusError("Upstream proxy rejected the request body", status_code=413)
    from reyn.services.compaction.engine import _CONTEXT_OVERFLOW_KEYWORDS
    assert not any(kw in exc.args[0].lower() for kw in _CONTEXT_OVERFLOW_KEYWORDS)

    assert is_context_overflow_error(exc) is True


def test_a_non_413_status_code_with_no_keywords_is_not_overflow() -> None:
    """Tier 2: #4381 A accept-side — a differently-coded HTTP error (400,
    not 413) with no overflow-shaped wording is NOT classified as
    overflow. The 413 check must not become a blanket "any status_code
    means overflow" — it targets exactly 413."""
    exc = _FakeStatusError("Bad request", status_code=400)
    assert is_context_overflow_error(exc) is False


def test_413_classification_does_not_require_litellm_to_be_importable(monkeypatch) -> None:
    """Tier 2: #4381 A — the status_code check must not depend on litellm
    being importable (a plain attribute read needs no litellm dependency
    at all); simulates litellm import failure and confirms 413 is still
    caught."""
    import reyn.services.compaction.engine as engine_mod

    def _raise_import_error():
        raise ImportError("simulated: litellm unavailable")

    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready", _raise_import_error,
    )
    exc = _FakeStatusError("Request Entity Too Large", status_code=413)
    assert engine_mod.is_context_overflow_error(exc) is True


def test_deepest_cause_walks_a_multi_level_chain() -> None:
    """Tier 2: #4381 B — _deepest_cause returns the ROOT of a chain, not
    just the immediate __cause__, matching retry_loop's own multi-level
    wrap-and-re-raise shape (CompactionOverflowError/ContextOverflowError
    wrapping the original provider exception, then re-wrapped again by
    router_loop_driver's own "Router context overflow after bounded
    shrink" ContextOverflowError)."""
    root = ValueError("Request Entity Too Large")
    middle = RuntimeError("compaction overflow")
    middle.__cause__ = root
    outer = RuntimeError("router context overflow after bounded shrink")
    outer.__cause__ = middle

    result = _deepest_cause(outer)

    assert result is root
    assert type(result).__name__ == "ValueError"


def test_deepest_cause_returns_none_when_there_is_no_cause() -> None:
    """Tier 2: #4381 B accept-side — an exception that IS its own root
    (no __cause__ at all — the common case for most exceptions) returns
    None, not itself. The caller (Session._run_router_loop) uses this to
    decide whether to print anything extra at all; returning the
    exception itself here would make every plain exception look like it
    has a "different" cause from itself."""
    plain = ValueError("just a plain error, never wrapped")
    assert _deepest_cause(plain) is None
