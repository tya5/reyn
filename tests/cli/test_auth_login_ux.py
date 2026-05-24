"""Tier 2: `reyn auth login` device-grant UX surface (issue #291 Priority 1).

Pins the new user-facing display contract for the RFC 8628 device grant
flow's interactive step:

  1. ``_box_user_code`` renders the user code inside a unicode box
     (= visual emphasis, phishing-protection per RFC 8628 §3.3.1).
  2. ``_print_user_action`` displays:
       - the verification URL (prefers ``verification_uri_complete``)
       - the boxed user_code
       - the ``expires_in`` deadline (minutes)
  3. ``_open_browser_or_skip`` opens the browser only when stdin is a
     TTY and ``REYN_AUTH_NO_BROWSER`` is unset.

Tests spy on ``webbrowser.open`` + ``input`` via direct attribute
substitution (= no ``unittest.mock``; see ``docs/deep-dives/contributing/testing.ja.md``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.cli.commands import auth as auth_mod

# ── _box_user_code ──────────────────────────────────────────────────────────


def test_box_user_code_renders_three_line_unicode_box() -> None:
    """Tier 2: box has top border, code line, bottom border (= 3 lines)."""
    out = auth_mod._box_user_code("ABCD-EFGH")
    top, mid, bot = out.splitlines()  # exactly 3 lines: top border / code / bottom border
    assert "┌" in top and "┐" in top
    assert "ABCD-EFGH" in mid
    assert "└" in bot and "┘" in bot


def test_box_user_code_border_width_matches_inner_width() -> None:
    """Tier 2: border ── count equals the inner padded-code width.

    A mismatch would visually skew the box. Tests use unicode line-
    drawing chars so we count code points, not byte widths.
    """
    code = "ABCD-EFGH"
    out = auth_mod._box_user_code(code)
    top, mid, bot = out.splitlines()
    # Strip the indent + corner chars to compare inner widths.
    top_border = top.strip().strip("┌┐")
    bot_border = bot.strip().strip("└┘")
    mid_inner = mid.strip().strip("│")
    assert len(top_border) == len(mid_inner)
    assert len(bot_border) == len(mid_inner)


def test_box_user_code_empty_falls_back_gracefully() -> None:
    """Tier 2: empty/missing code → `(no user code)` placeholder, no crash."""
    assert "(no user code)" in auth_mod._box_user_code("")


# ── _print_user_action ──────────────────────────────────────────────────────


def test_print_user_action_shows_boxed_code_and_url_and_deadline(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: stderr output contains URL, boxed code, and `Code expires in N minutes`."""
    # Suppress the browser auto-open path (= we cover that in its own test).
    monkeypatch.setattr(auth_mod, "_open_browser_or_skip", lambda _url: None)

    info = {
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
        "expires_in": 1800,
    }
    auth_mod._print_user_action(info)

    err = capsys.readouterr().err
    assert "https://example.com/device" in err
    assert "WDJB-MJHT" in err
    assert "┌" in err and "└" in err  # box drawing present
    assert "Code expires in 30 minutes" in err


def test_print_user_action_prefers_verification_uri_complete(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when ``verification_uri_complete`` is present, it is the
    URL displayed (= single-click flow; the bare verification_uri is
    redundant in that case)."""
    monkeypatch.setattr(auth_mod, "_open_browser_or_skip", lambda _url: None)

    info = {
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
        "verification_uri_complete": "https://example.com/device?user_code=WDJB-MJHT",
        "expires_in": 600,
    }
    auth_mod._print_user_action(info)

    err = capsys.readouterr().err
    assert "https://example.com/device?user_code=WDJB-MJHT" in err


def test_print_user_action_passes_chosen_url_to_browser_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the URL handed to ``_open_browser_or_skip`` matches the URL
    rendered in the text (= preference for verification_uri_complete is
    consistent across display + auto-open)."""
    opened: list[str] = []

    def _recorder(url: str) -> None:
        opened.append(url)

    monkeypatch.setattr(auth_mod, "_open_browser_or_skip", _recorder)

    info = {
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
        "verification_uri_complete": "https://example.com/device?user_code=WDJB-MJHT",
        "expires_in": 600,
    }
    auth_mod._print_user_action(info)

    assert opened == ["https://example.com/device?user_code=WDJB-MJHT"]


def test_print_user_action_omits_deadline_when_expires_in_missing(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: missing ``expires_in`` → no `Code expires in ...` line
    (= back-compat for callers / tests on the old callback dict)."""
    monkeypatch.setattr(auth_mod, "_open_browser_or_skip", lambda _url: None)

    info = {
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
    }
    auth_mod._print_user_action(info)

    err = capsys.readouterr().err
    assert "Code expires in" not in err


# ── _open_browser_or_skip ───────────────────────────────────────────────────


def test_open_browser_skips_when_stdin_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: non-TTY stdin (= pipe, script, CI) → no prompt, no open."""

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", _FakeStdin())

    opened: list[str] = []
    monkeypatch.setattr(
        auth_mod.webbrowser, "open", lambda url: opened.append(url) or True,
    )

    auth_mod._open_browser_or_skip("https://example.com/device")
    assert opened == []


def test_open_browser_skips_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ``REYN_AUTH_NO_BROWSER`` set → user opt-out honoured."""

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.setenv("REYN_AUTH_NO_BROWSER", "1")

    opened: list[str] = []
    monkeypatch.setattr(
        auth_mod.webbrowser, "open", lambda url: opened.append(url) or True,
    )

    auth_mod._open_browser_or_skip("https://example.com/device")
    assert opened == []


def test_open_browser_invokes_webbrowser_open_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: TTY + no opt-out + Enter pressed → ``webbrowser.open(url)`` called."""

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.delenv("REYN_AUTH_NO_BROWSER", raising=False)
    # input() must return without blocking.
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    opened: list[str] = []
    monkeypatch.setattr(
        auth_mod.webbrowser, "open", lambda url: opened.append(url) or True,
    )

    auth_mod._open_browser_or_skip("https://example.com/device")
    assert opened == ["https://example.com/device"]


def test_open_browser_eof_during_prompt_returns_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ``EOFError`` from input() (= stdin closed mid-prompt) → no
    crash, no browser open (= manual fallback URL is already printed)."""

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.delenv("REYN_AUTH_NO_BROWSER", raising=False)

    def _eof_input(*_a, **_kw):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)

    opened: list[str] = []
    monkeypatch.setattr(
        auth_mod.webbrowser, "open", lambda url: opened.append(url) or True,
    )

    auth_mod._open_browser_or_skip("https://example.com/device")
    assert opened == []


def test_open_browser_swallows_webbrowser_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ``webbrowser.open`` raising → caller-visible no-op (URL was
    already printed for manual fallback; UX must not crash on bad
    headless env)."""

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.delenv("REYN_AUTH_NO_BROWSER", raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    def _boom(_url: str) -> bool:
        raise RuntimeError("no display")

    monkeypatch.setattr(auth_mod.webbrowser, "open", _boom)

    # No exception leaks.
    auth_mod._open_browser_or_skip("https://example.com/device")
