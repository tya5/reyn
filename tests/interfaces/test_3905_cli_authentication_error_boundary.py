"""Tier 2: #3905 — the CLI's narrow AuthenticationError boundary.

lead-coder's review correction on #3929: removing the #2708 P3.2b
credential-error boundary entirely (owner's "delete the hardcode" ruling)
is a real UX regression for missing-key runs — reyn no longer knows the env
var NAME, so the message becomes a raw traceback instead of a one-liner.
This narrows that boundary to a real `litellm.exceptions.AuthenticationError`
`isinstance` check (no hardcoded provider/env-var lookup table) rather than
restoring the old typed pre-check.

The boundary is an ``is_litellm_ready()`` gate BEFORE reading
``litellm.exceptions`` — reading it while litellm was never imported would
otherwise force litellm's full cold import (measured directly: ~1.76s
cold), so checking it unconditionally would tax every unrelated late
failure with an irrelevant multi-second cost. Real
``litellm.exceptions.AuthenticationError`` instances throughout — no mocks
of litellm's own exception hierarchy.

#4395/#4421 (architect finding): this boundary used to gate on
``"litellm" in sys.modules`` — Python places a module into ``sys.modules``
at the START of import, before its top-level code finishes, so that check
only ever proved the import STARTED, not that it FINISHED; #4417's
background warming thread turned that into a live race (the SAME shape
#4423 fixed at `llm.py`'s `_is_llm_timeout_exc`, the actual site the
owner's `AttributeError: module 'litellm' has no attribute 'exceptions'`
traced to). Fixed the same way: gate on ``is_litellm_ready()`` and read
``AuthenticationError`` off the confirmed module via ``sys.modules``,
never a fresh ``from litellm... import``.
"""
from __future__ import annotations

import argparse
import sys

import pytest

import reyn.llm.litellm_bootstrap as lb_mod


def _fake_parser(target_exc: Exception) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    def _boom(_args) -> None:
        raise target_exc

    parser.set_defaults(func=_boom)
    return parser


def test_authentication_error_renders_friendly_and_exits(monkeypatch, capsys) -> None:
    """Tier 2: a real litellm.exceptions.AuthenticationError renders as
    ``Error: <message>`` + exit 1, not a raw traceback."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    # This test's own precondition, stated explicitly rather than left to
    # incidental test-order luck: the boundary only reads litellm's
    # exception hierarchy once `is_litellm_ready()` confirms it, so a real
    # completion must have already imported it — via the chokepoint, not
    # a bare `from litellm.exceptions import AuthenticationError` (which
    # would leave `is_litellm_ready()` False even though litellm is
    # genuinely importable).
    litellm = ensure_litellm_ready()
    assert litellm is not None, "this test requires a real litellm install"

    import reyn.interfaces.cli as cli

    exc = litellm.exceptions.AuthenticationError(
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
    """Tier 2: an unrelated exception is NOT caught by the boundary — it
    propagates unmodified, exactly as it would with no boundary at all.
    Independent of litellm's readiness state (a ``ValueError`` is never an
    ``AuthenticationError`` instance either way)."""
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
    is deliberately NOT caught, even with litellm genuinely ready. If the
    isinstance check were accidentally broadened (e.g. to a bare
    ``except Exception``), this would go RED (SystemExit instead of the
    raw exception)."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    litellm = ensure_litellm_ready()
    assert litellm is not None, "this test requires a real litellm install"

    import reyn.interfaces.cli as cli

    exc = litellm.exceptions.InternalServerError(
        message="Missing credentials",
        llm_provider="openai", model="gpt-4o-mini",
    )
    monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
    monkeypatch.setattr(sys, "argv", ["reyn"])

    with pytest.raises(litellm.exceptions.InternalServerError):
        cli.main()


def test_the_litellm_exceptions_read_is_skipped_when_litellm_was_never_loaded(
    monkeypatch,
) -> None:
    """Tier 2: the ``is_litellm_ready()`` gate itself — when litellm was
    never imported into this process, the boundary must not touch it just
    to run the isinstance check (the exact cost #3671 measures startup
    against). Verified by simulating "never touched" BOTH ways: removing
    'litellm' from `sys.modules` AND resetting the chokepoint's own
    `_litellm_ready` flag (restored after) — the flag alone would
    otherwise leak True from an earlier test in this same file/session
    (a process-global) even after `sys.modules` is stripped, silently
    reintroducing the exact race this boundary exists to avoid (`sys.
    modules["litellm"]` on a name that was just removed) instead of
    exercising the not-ready path this test means to cover."""
    import reyn.interfaces.cli as cli

    had_litellm = "litellm" in sys.modules
    saved = {k: v for k, v in sys.modules.items() if k == "litellm" or k.startswith("litellm.")}
    for k in list(saved):
        del sys.modules[k]
    original_ready = lb_mod._litellm_ready
    lb_mod._litellm_ready = False
    lb_mod._ready_registry.pop("ready", None)
    try:
        assert "litellm" not in sys.modules

        exc = RuntimeError("some other late failure")
        monkeypatch.setattr(cli, "build_parser", lambda: _fake_parser(exc))
        monkeypatch.setattr(sys, "argv", ["reyn"])

        with pytest.raises(RuntimeError, match="some other late failure"):
            cli.main()

        assert "litellm" not in sys.modules, (
            "the boundary imported litellm.exceptions for an exception that "
            "was never litellm's — the is_litellm_ready() gate did not "
            "prevent it"
        )
    finally:
        lb_mod._litellm_ready = original_ready
        lb_mod._ready_registry.pop("ready", None)
        if had_litellm:
            sys.modules.update(saved)
