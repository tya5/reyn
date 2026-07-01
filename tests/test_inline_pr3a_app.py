"""Tier 2: inline app input driver — working-row fragments + input-path routing flag.

The Application itself is an interactive driver verified live (e2e); here we pin
the pure fragment builder and the renderer capability flag that selects the app
input path. Assertions are on public return values, not whitespace/private state.
"""
from __future__ import annotations

from datetime import datetime, timezone

from reyn.interfaces.inline.app import working_line
from reyn.interfaces.repl.renderer import (
    ChatRenderer,
    ConsoleChatRenderer,
    InlineChatRenderer,
)
from reyn.schemas.models import Event


def _evt(t: str) -> Event:
    return Event(type=t, timestamp=datetime.now(timezone.utc), data={})


def test_working_line_idle_is_empty() -> None:
    """Tier 2: no working row when a turn is not running."""
    assert working_line(False, 0.0, 5.0) == []


def test_working_line_running_has_spinner_and_label() -> None:
    """Tier 2: while running, the row carries a spinner glyph + 'Working…'."""
    frags = working_line(True, 0.0, 3.0)
    text = "".join(t for _, t in frags)
    assert "Working" in text
    assert "3s" in text          # elapsed = now - start
    assert text.strip()[0] not in ("W",)  # a spinner glyph leads, not the label


def test_working_line_elapsed_tracks_now_minus_start() -> None:
    """Tier 2: elapsed seconds = floor(now - think_start)."""
    text = "".join(t for _, t in working_line(True, 10.0, 17.4))
    assert "7s" in text


def test_working_line_never_negative_elapsed() -> None:
    """Tier 2: a clock skew (now < start) clamps elapsed to 0, not negative."""
    text = "".join(t for _, t in working_line(True, 10.0, 9.0))
    assert "0s" in text
    assert "Working… -" not in text  # no negative sign before elapsed seconds


def test_working_line_has_a_moving_shimmer_crest() -> None:
    """Tier 2: the label carries a bright shimmer crest whose position sweeps with
    the clock (it animates), not a static dim line."""
    def crest_char(now: float):
        # The crest is the lone bold fragment among the label characters.
        for style, text in working_line(True, 0.0, now):
            if "bold" in style and text.strip():
                return text
        return None
    c0 = crest_char(0.0)    # head at char 0
    c1 = crest_char(0.10)   # head advances → crest on a later char
    assert c0 is not None and c1 is not None  # a crest exists (shimmer present)
    assert c0 != c1                            # and it moved (animated)


def test_inline_renderer_selects_app_input() -> None:
    """Tier 2: the interactive inline renderer drives input via its own app."""
    assert InlineChatRenderer().uses_app_input() is True


def test_plain_renderers_keep_promptsession_path() -> None:
    """Tier 2: plain / base renderers stay on the PromptSession _input_loop."""
    assert ConsoleChatRenderer().uses_app_input() is False
    assert ChatRenderer().uses_app_input() is False


def test_turn_settled_clears_indicator_after_short_circuit_turn() -> None:
    """Tier 2: turn_settled clears the working indicator even when no
    turn_completed fired (slash / intervention short-circuit paths)."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    assert r.bottom_toolbar() is not None
    # A slash turn ends with turn_settled (no turn_completed) — must clear.
    r.on_chat_event(_evt("turn_settled"))
    assert r.bottom_toolbar() is None


# ── working_line cancelling-state tests ─────────────────────────────────────


def test_working_line_normal_shows_interrupt_affordance() -> None:
    """Tier 2: normal working row includes 'ctrl-c to interrupt' hint."""
    text = "".join(t for _, t in working_line(True, 0.0, 3.0))
    assert "ctrl-c" in text


def test_working_line_cancelling_shows_cancelling_text() -> None:
    """Tier 2: when cancelling=True the row shows 'Cancelling' not the shimmer."""
    frags = working_line(True, 0.0, 3.0, cancelling=True)
    text = "".join(t for _, t in frags)
    assert "Cancelling" in text
    # Shimmer elements gone: no spinner, no elapsed seconds.
    assert "Working" not in text
    assert "ctrl-c" not in text


def test_working_line_idle_cancelling_is_empty() -> None:
    """Tier 2: idle (thinking=False) returns [] even when cancelling=True."""
    assert working_line(False, 0.0, 3.0, cancelling=True) == []


def test_cancelling_state_does_not_bleed_into_next_turn() -> None:
    """Tier 2: a ctrl-c cancel in one turn does not show 'Cancelling…' in the next.

    The ConditionalContainer hides the working row when _thinking=False, so the old
    clear-in-_working_frags path was dead code. on_chat_event must reset the flag on
    turn end so it never leaks. Verified via the working_line output (public surface):
    after cancel + turn-end + new turn-start, the working row shows the normal shimmer,
    not the cancellation indicator.
    """
    for end_event in ("turn_settled", "turn_completed", "turn_cancelled"):
        r = InlineChatRenderer()
        r.on_chat_event(_evt("turn_started"))
        r._cancelling = True  # simulate: user pressed ctrl-c mid-turn
        r.on_chat_event(_evt(end_event))

        # The next turn starts — read the working row with the renderer's current state.
        frags = working_line(True, 0.0, 3.0, cancelling=r._cancelling)
        text = "".join(t for _, t in frags)
        assert "Cancelling" not in text, (
            f"after {end_event}, next turn still shows Cancelling indicator"
        )
        assert "Working" in text, (
            f"after {end_event}, next turn should show normal working indicator"
        )
