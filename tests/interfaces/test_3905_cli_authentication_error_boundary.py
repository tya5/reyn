"""Tier 2: #3905 — the CLI's narrow AuthenticationError boundary.

lead-coder's review correction on #3929: removing the #2708 P3.2b
credential-error boundary entirely (owner's "delete the hardcode" ruling)
is a real UX regression for missing-key runs — reyn no longer knows the env
var NAME, so the message becomes a raw traceback instead of a one-liner.
This narrows that boundary to a real `litellm.exceptions.AuthenticationError`
`isinstance` check (no hardcoded provider/env-var lookup table) rather than
restoring the old typed pre-check.

The boundary is a ``sys.modules`` gate BEFORE importing
``litellm.exceptions`` — importing it pulls in all of litellm (measured
directly: ~1.76s cold), so checking it unconditionally would tax every
unrelated late failure with an irrelevant multi-second cost. Real
``litellm.exceptions.AuthenticationError`` instances throughout — no mocks
of litellm's own exception hierarchy.
"""
from __future__ import annotations

import argparse
import sys

import pytest


def _fake_parser(target_exc: Exception) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    def _boom(_args) -> None:
        raise target_exc

    parser.set_defaults(func=_boom)
    return parser


def test_authentication_error_renders_friendly_and_exits(monkeypatch, capsys) -> None:
    """Tier 2: a real litellm.exceptions.AuthenticationError renders as
    ``Error: <message>`` + exit 1, not a raw traceback."""
    from litellm.exceptions import AuthenticationError

    import reyn.interfaces.cli as cli

    exc = AuthenticationError(
        message="Missing Anthropic API Key - set ANTHROPIC_API_KEY",
        llm_provider="anthropic", model="claude-3-5-haiku-20241022",
    )
    monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
    monkeypatch.setattr(sys, "argv", ["reyn"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert err.startswith("Error: ")


def test_a_non_litellm_exception_propagates_as_a_normal_traceback(monkeypatch) -> None:
    """Tier 2: an unrelated exception (litellm never touched, never
    imported into this process by this test) is NOT caught by the
    boundary — it propagates unmodified, exactly as it would with no
    boundary at all. Falsify-relevant: this is the case the sys.modules
    gate exists to keep cheap and untouched."""
    import reyn.interfaces.cli as cli

    exc = ValueError("unrelated bug, nothing to do with credentials")
    monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
    monkeypatch.setattr(sys, "argv", ["reyn"])

    with pytest.raises(ValueError, match="unrelated bug"):
        cli.main()


def test_an_internal_server_error_is_not_caught_the_boundary_is_narrow(
    monkeypatch,
) -> None:
    """Tier 2: strip-falsify the boundary's OWN narrowness claim — an
    InternalServerError (openai's real missing-key shape, #3905 measured)
    is deliberately NOT caught, even with litellm already imported. If the
    isinstance check were accidentally broadened (e.g. to a bare
    ``except Exception``), this would go RED (SystemExit instead of the
    raw exception)."""
    import litellm  # noqa: F401 - ensure litellm IS in sys.modules for this case
    from litellm.exceptions import InternalServerError

    import reyn.interfaces.cli as cli

    exc = InternalServerError(
        message="Missing credentials",
        llm_provider="openai", model="gpt-4o-mini",
    )
    monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
    monkeypatch.setattr(sys, "argv", ["reyn"])

    with pytest.raises(InternalServerError):
        cli.main()


def test_the_litellm_exceptions_import_is_skipped_when_litellm_was_never_loaded(
    monkeypatch,
) -> None:
    """Tier 2: the sys.modules gate itself — when litellm was never
    imported into this process, the boundary must not import it just to
    run the isinstance check (the exact cost #3671 measures startup
    against). Verified by removing 'litellm' from sys.modules for the
    duration of this test (restored after) and confirming it STAYS absent
    once the unrelated exception is handled."""
    import reyn.interfaces.cli as cli

    had_litellm = "litellm" in sys.modules
    saved = {k: v for k, v in sys.modules.items() if k == "litellm" or k.startswith("litellm.")}
    for k in list(saved):
        del sys.modules[k]
    try:
        assert "litellm" not in sys.modules

        exc = RuntimeError("some other late failure")
        monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
        monkeypatch.setattr(sys, "argv", ["reyn"])

        with pytest.raises(RuntimeError, match="some other late failure"):
            cli.main()

        assert "litellm" not in sys.modules, (
            "the boundary imported litellm.exceptions for an exception that "
            "was never litellm's — the sys.modules gate did not prevent it"
        )
    finally:
        if had_litellm:
            sys.modules.update(saved)
