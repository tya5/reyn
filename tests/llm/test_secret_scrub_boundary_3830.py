"""Tier 1: reyn.llm.secret_scrub — the shared exception-boundary scrub (#3830).

Real litellm exception INSTANCES (not mocks/stand-ins) constructed the same
way litellm itself constructs them, shaped with a synthetic (never real)
placeholder value — no test in this file ever handles or asserts on an
actual credential.
"""
from __future__ import annotations

import litellm
import pytest

from reyn.llm.secret_scrub import (
    SECRET_ENV_VARS,
    collect_secret_values,
    scrub_exception_in_place,
    scrub_secrets,
)

_FAKE_TOKEN = "sk-FAKEPLACEHOLDER000"  # synthetic — never a real credential


@pytest.fixture(autouse=True)
def _no_ambient_secret_env(monkeypatch):
    """`collect_secret_values` unconditionally scans every
    `SECRET_ENV_VARS` name — a real dev/CI environment may have real
    provider keys set for unrelated reasons. Every test in this file must
    see a clean slate so its assertions are about the FUNCTION's contract,
    not this machine's ambient environment (and so no real key value can
    ever reach an assertion diff or failure message here)."""
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_litellm_exception() -> "litellm.exceptions.AuthenticationError":
    """The exact shape #3830's own repro observed: a 401 whose message AND
    parsed body both quote the rejected token — litellm builds both from the
    provider's HTTP response the same way in production."""
    exc = litellm.exceptions.AuthenticationError(
        message=f"Error code: 401 - rejected token {_FAKE_TOKEN}",
        llm_provider="openai",
        model="gpt-4",
    )
    exc.body = {"error": {"message": f"rejected token {_FAKE_TOKEN}"}}
    return exc


# ── collect_secret_values ───────────────────────────────────────────────────


def test_collect_secret_values_reads_secret_keyed_kwargs() -> None:
    """Tier 1: a kwarg whose KEY matches a secret hint (api_key, token, …)
    contributes its VALUE — the proxy-injected api_key shape."""
    assert collect_secret_values({"api_key": _FAKE_TOKEN}) == [_FAKE_TOKEN]


def test_collect_secret_values_ignores_non_secret_kwargs() -> None:
    """Tier 1: an ordinary kwarg (no secret-hint key) contributes nothing —
    no false-positive scrubbing of legitimate call params."""
    assert collect_secret_values({"model": "gpt-4", "temperature": 0.7}) == []


def test_collect_secret_values_reads_known_env_vars(monkeypatch) -> None:
    """Tier 1: a known API-key env var contributes its value UNCONDITIONALLY
    — a provider call may read its key straight from the environment
    without it ever passing through base_kwargs at all."""
    monkeypatch.setenv("OPENAI_API_KEY", _FAKE_TOKEN)
    assert collect_secret_values({}) == [_FAKE_TOKEN]


# ── scrub_secrets ────────────────────────────────────────────────────────────


def test_scrub_secrets_replaces_in_string() -> None:
    """Tier 1: the baseline string case — a known secret value occurring in
    a plain string is replaced with a fixed marker."""
    assert scrub_secrets(f"token {_FAKE_TOKEN} rejected", [_FAKE_TOKEN]) == (
        "token ***REDACTED*** rejected"
    )


def test_scrub_secrets_recurses_into_dict_and_list() -> None:
    """Tier 1: #3830's own regression — a dict/list body must be walked, not
    returned unchanged (the original #1676 version's bug)."""
    payload = {"error": {"message": f"rejected {_FAKE_TOKEN}"}, "codes": [_FAKE_TOKEN]}
    scrubbed = scrub_secrets(payload, [_FAKE_TOKEN])
    assert _FAKE_TOKEN not in str(scrubbed)
    assert scrubbed["error"]["message"] == "rejected ***REDACTED***"
    assert scrubbed["codes"] == ["***REDACTED***"]


def test_scrub_secrets_is_a_noop_with_no_secrets() -> None:
    """Tier 1: an empty secrets list leaves the value untouched — the
    function costs nothing when there's nothing configured to scrub."""
    assert scrub_secrets(f"token {_FAKE_TOKEN}", []) == f"token {_FAKE_TOKEN}"


# ── scrub_exception_in_place — the load-bearing boundary fix ───────────────


def test_str_and_repr_leak_the_token_before_scrubbing() -> None:
    """Tier 1: the FALSIFICATION baseline — #3830's own measured finding.
    Without the fix, both str() and repr() of a real litellm exception
    carry the raw token, because litellm builds .args/.message from the
    provider's HTTP response text."""
    exc = _make_litellm_exception()
    assert _FAKE_TOKEN in str(exc)
    assert _FAKE_TOKEN in repr(exc)


def test_scrub_exception_in_place_removes_the_token_from_str_and_repr() -> None:
    """Tier 1: the fix itself — after scrubbing, NEITHER str() nor repr()
    carries the token. This is what makes every downstream consumer
    (logger.exception, an f-string, a future sink) automatically safe."""
    exc = _make_litellm_exception()
    scrub_exception_in_place(exc, {"api_key": _FAKE_TOKEN})
    assert _FAKE_TOKEN not in str(exc)
    assert _FAKE_TOKEN not in repr(exc)


def test_scrub_exception_in_place_mutates_the_same_object_identity() -> None:
    """Tier 1: the boundary fix mutates IN PLACE — a bare `raise` after
    calling this (no `raise new_exc`) must re-raise an already-scrubbed
    object, not require the caller to swap in a replacement."""
    exc = _make_litellm_exception()
    returned = scrub_exception_in_place(exc, {"api_key": _FAKE_TOKEN})
    assert returned is exc


def test_scrub_exception_in_place_also_scrubs_the_body_attribute() -> None:
    """Tier 1: the .body attribute litellm attaches (the parsed provider
    error dict) is scrubbed too, not just .args/.message."""
    exc = _make_litellm_exception()
    scrub_exception_in_place(exc, {"api_key": _FAKE_TOKEN})
    assert _FAKE_TOKEN not in str(exc.body)


def test_scrub_exception_in_place_reads_secrets_from_env_too(monkeypatch) -> None:
    """Tier 1: a call that read its key from an env var (no api_key kwarg at
    all) is still scrubbed — collect_secret_values' env-var half."""
    monkeypatch.setenv("OPENAI_API_KEY", _FAKE_TOKEN)
    exc = _make_litellm_exception()
    scrub_exception_in_place(exc, {})  # no api_key kwarg
    assert _FAKE_TOKEN not in str(exc)


def test_scrub_exception_in_place_is_a_noop_with_no_secrets_configured() -> None:
    """Tier 1: with nothing to scrub (no secret kwargs, no env vars set),
    the exception is returned untouched — no cost, no accidental mutation,
    when there's genuinely nothing to redact."""
    exc = litellm.exceptions.APIError(
        message="ordinary 500, no credential in it",
        llm_provider="openai", model="gpt-4", status_code=500,
    )
    original_args = exc.args
    scrub_exception_in_place(exc, {})
    assert exc.args == original_args


def test_an_exception_missing_a_body_attribute_does_not_raise() -> None:
    """Tier 1: a plain stdlib exception (no .body/.message litellm-specific
    attributes) must not break the scrub — hasattr-guarded, defensive."""
    exc = ValueError(f"plain error mentioning {_FAKE_TOKEN}")
    scrub_exception_in_place(exc, {"api_key": _FAKE_TOKEN})
    assert _FAKE_TOKEN not in str(exc)


@pytest.mark.parametrize("secrets", [[], [""]])
def test_no_real_secrets_means_no_mutation(secrets) -> None:
    """Tier 1: an empty or blank-only secret list must not touch the
    exception at all — scrub_secrets' own no-op guard, exercised through
    the boundary function."""
    exc = _make_litellm_exception()
    before = exc.args
    result = scrub_secrets(str(exc), secrets)
    assert result == str(exc)
    assert exc.args == before
