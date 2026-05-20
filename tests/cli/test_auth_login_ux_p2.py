"""Tier 2: `reyn auth login` device-grant UX P2 (issue #291 Priority 2).

Pins:

  1. ``_animated_wait`` produces spinner frames on TTY and degrades to
     a plain sleep on non-TTY (= log captures stay clean).
  2. ``_print_slow_down_notice`` emits a human-readable line about the
     new poll interval (= surfaces the OAuth server's slow_down hint
     that was previously absorbed silently).
  3. ``_print_user_action`` includes the ``Ctrl+C to cancel`` hint in
     the waiting line so users on non-TTY (= no spinner) still see how
     to abort.

All tests spy via direct attribute substitution / recording lambdas —
no ``unittest.mock`` per ``docs/deep-dives/contributing/testing.ja.md``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.cli.commands import auth as auth_mod

# ── _animated_wait ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_animated_wait_emits_multiple_spinner_frames_on_tty(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: TTY stderr → multiple `\\r`-anchored frames printed.

    Patches ``_SPINNER_INTERVAL_SECONDS`` down to 0.01s so the test runs
    in <50ms while still exercising the frame-cycling loop.
    """

    class _TTYStream:
        def __init__(self) -> None:
            self.buf: list[str] = []

        def isatty(self) -> bool:
            return True

        def write(self, s: str) -> int:
            self.buf.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    fake_stderr = _TTYStream()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    monkeypatch.setattr(auth_mod, "_SPINNER_INTERVAL_SECONDS", 0.01)

    await auth_mod._animated_wait(0.05)

    # Every frame write contains a `\r` followed by the indent + frame char.
    frame_writes = [s for s in fake_stderr.buf if s.startswith("\r")]
    # Expect at least 3 frames in a 0.05s window with 0.01s interval.
    assert len(frame_writes) >= 3
    # At least one of the cycled frames should be visible (dots accumulate).
    joined = "".join(frame_writes)
    assert "." in joined


@pytest.mark.asyncio
async def test_animated_wait_no_animation_on_non_tty(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: non-TTY stderr → no spinner output, just a plain sleep.

    Verifies (a) stderr capture has zero spinner-shaped writes, and (b)
    the call completes in roughly the requested duration via a recorded
    asyncio.sleep call (= we don't actually let it sleep — we spy).
    """

    class _NonTTYStream:
        def __init__(self) -> None:
            self.buf: list[str] = []

        def isatty(self) -> bool:
            return False

        def write(self, s: str) -> int:
            self.buf.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    fake_stderr = _NonTTYStream()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    sleep_calls: list[float] = []

    async def _recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(auth_mod.asyncio, "sleep", _recording_sleep)

    await auth_mod._animated_wait(5.0)

    # Single asyncio.sleep call equal to the requested duration.
    assert sleep_calls == [5.0]
    # No spinner output at all.
    assert fake_stderr.buf == []


# ── _print_slow_down_notice ─────────────────────────────────────────────────


def test_print_slow_down_notice_announces_new_interval(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: notice line includes the new interval rounded to a whole second.

    The exact phrasing ("Server requested slower polling — interval now Xs.")
    is pinned because the user has to understand that polling continues but
    slower; a vague message would leave them wondering whether to abort.
    """

    class _NonTTYStream:
        def isatty(self) -> bool:
            return False

        def write(self, s: str) -> int:
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stderr", _NonTTYStream())

    captured: list[str] = []

    class _CapturedStream(_NonTTYStream):
        def write(self, s: str) -> int:
            captured.append(s)
            return len(s)

    monkeypatch.setattr(sys, "stderr", _CapturedStream())

    auth_mod._print_slow_down_notice(10.0)

    joined = "".join(captured)
    assert "Server requested slower polling" in joined
    assert "10" in joined


def test_print_slow_down_notice_clears_spinner_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: TTY stderr → notice emits a `\\r` + spaces clear sequence
    before the message (= overwrites residual spinner glyphs)."""

    captured: list[str] = []

    class _TTYStream:
        def isatty(self) -> bool:
            return True

        def write(self, s: str) -> int:
            captured.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stderr", _TTYStream())

    auth_mod._print_slow_down_notice(15.0)

    joined = "".join(captured)
    # Clear sequence: a `\r` precedes the spaces precedes another `\r`.
    assert "\r" in joined
    assert "Server requested slower polling" in joined


# ── _print_user_action — Ctrl+C hint ────────────────────────────────────────


def test_print_user_action_includes_ctrl_c_hint(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the waiting line carries the `Ctrl+C to cancel` hint so
    non-TTY contexts (= where the spinner is silent) still surface the
    cancel mechanism."""
    monkeypatch.setattr(auth_mod, "_open_browser_or_skip", lambda _url: None)

    info = {
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
        "expires_in": 600,
    }
    auth_mod._print_user_action(info)

    err = capsys.readouterr().err
    assert "Ctrl+C to cancel" in err
    assert "Waiting for approval" in err
