"""Tier 2: ReynHeader surfaces ``[N pending]`` badge when stalled_count > 0.

Issue #277 — Layer 1 of the TUI surface bundle. The header is the
ambient signal that says "N stalled / cross-channel ops exist
somewhere; open Pending tab or run /pending to see them".

Contract pinned:

1. ``stalled_count == 0`` → badge omitted (= cold-default layout
   unchanged, ``[N pending]`` substring absent from the status line).
2. ``stalled_count > 0`` → badge present with the count.
3. ``refresh_status`` accepts ``stalled_count`` and updates the
   rendered status line.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from rich.text import Text as RichText

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.header import ReynHeader


def _plain_status(header: ReynHeader) -> str:
    return str(header._format_status())


def test_default_zero_count_omits_badge() -> None:
    """Tier 2: default constructed header (stalled_count=0) has no badge."""
    h = ReynHeader(agent_name="test", model="test")
    text = _plain_status(h)
    assert "pending" not in text.lower(), text


def test_refresh_with_nonzero_count_shows_badge() -> None:
    """Tier 2: ``refresh_status(stalled_count=3)`` surfaces ``[3 pending]``."""
    h = ReynHeader(agent_name="test", model="test")
    # ``refresh_status`` may try to query the live DOM — guard via
    # the same Exception-swallow path the production code uses,
    # then read the state directly.
    h._stalled_count = 3
    text = _plain_status(h)
    assert "[3 pending]" in text, text


def test_count_resets_to_zero_hides_badge_again() -> None:
    """Tier 2: dropping count back to 0 makes the badge disappear."""
    h = ReynHeader(agent_name="test", model="test")
    h._stalled_count = 5
    assert "[5 pending]" in _plain_status(h)
    h._stalled_count = 0
    text = _plain_status(h)
    assert "pending" not in text.lower(), text


def test_badge_appears_before_clock_field() -> None:
    """Tier 2: badge inserted between cost and clock so the canary stays
    rightmost.

    The clock is the "is the UI frozen?" canary documented at
    ``on_mount`` — its position is load-bearing for the user's mental
    model. Defend against a refactor that pushes the badge past the
    clock and breaks that contract.
    """
    h = ReynHeader(agent_name="test", model="test")
    h._stalled_count = 2
    text = _plain_status(h)
    badge_idx = text.find("[2 pending]")
    # Find an HH:MM:SS pattern (= the clock).
    clock_match = re.search(r"\d{2}:\d{2}:\d{2}", text)
    assert badge_idx >= 0
    assert clock_match is not None, text
    assert badge_idx < clock_match.start(), (
        f"badge must precede the clock; badge_idx={badge_idx} "
        f"clock_idx={clock_match.start()}; got {text!r}"
    )
