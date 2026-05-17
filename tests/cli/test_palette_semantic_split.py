"""Tier 2: palette splits coral into action-affordance vs amber agent-identity.

Visual UX audit (HIGH severity Finding F1): a single ``_CORAL`` accent
was used for agent identity (header label, streaming cursor,
intervention prefix), interactive affordances (``/expand`` hint, picker
selection caret, panel cursor ``▶``), and status glyphs. The eye saw
coral everywhere with no semantic rule.

The split:

  • ``_AMBER`` = agent identity (header label, streaming cursor ``▍``,
    intervention "Aria asks" prefix). One read: "this names the agent".
  • ``_CORAL`` = interactive affordance / "you are here" (``/expand``
    hint, fold markers, picker selection caret, panel cursor ``▶``).
    One read: "you can act here / your cursor is here".

These tests pin both reads — palette values exist and are distinct, and
the load-bearing render paths (agent header, streaming cursor, fold
hint) use the right one of the two.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.color import Color as _RichColor

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui._palette import _AMBER, _CORAL
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── palette: two distinct named colours ──────────────────────────────────────


def test_amber_and_coral_are_distinct() -> None:
    """Palette declares both names and they must not collide.

    A drift back to a single accent (``_AMBER = _CORAL``) would silently
    undo the entire semantic split, so pin the inequality.
    """
    assert _AMBER != _CORAL, (
        f"_AMBER and _CORAL must be distinct hex strings; got both = {_AMBER!r}"
    )
    # Both must be valid hex colour strings
    assert _AMBER.startswith("#") and len(_AMBER) == 7
    assert _CORAL.startswith("#") and len(_CORAL) == 7


def test_amber_is_in_warm_hue_family() -> None:
    """Amber should sit in a warm hue family (R dominant, B muted).

    Defence against a future palette nudge accidentally swapping amber
    for a cool hue (teal / blue) that would conflict with the user
    header colour and confuse the agent-identity read.
    """
    r, g, b = _RichColor.parse(_AMBER).triplet
    assert r >= g >= b, f"_AMBER must be warm (R≥G≥B); got rgb={r},{g},{b}"
    assert r >= 180, f"_AMBER red channel weak: {r}"
    assert b <= 140, f"_AMBER blue channel too high (not warm): {b}"


# ── agent-identity sites use _AMBER ──────────────────────────────────────────


def _find_first_with(strips, predicate) -> str:
    """Locate the first strip whose text passes ``predicate``; return its segs."""
    for strip in strips:
        text = "".join(seg.text for seg in strip)
        if predicate(text):
            return strip
    raise AssertionError("no strip matched predicate")


@pytest.mark.asyncio
async def test_agent_header_label_renders_with_amber() -> None:
    """The agent header label ``reyn`` carries the _AMBER colour.

    Checks the rendered Strip segments (= what hits the terminal) so a
    drift in the colour constant or a regression of the style string
    surfaces immediately.
    """
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Render an agent message so the "reyn" label appears in a header
        conv.render_message(OutboxMessage(kind="agent", text="hi"))
        await pilot.pause()

        # Find the strip containing "reyn" (the speaker label)
        strip = None
        for s in log.lines:
            text = "".join(seg.text for seg in s)
            if "reyn" in text and "─" in text:
                strip = s
                break
        assert strip is not None, "agent header strip not found"

        # The segment containing 'reyn' must carry the amber colour
        amber_lower = _AMBER.lower()
        found = False
        for seg in strip:
            if "reyn" in seg.text and seg.style is not None:
                style_str = str(seg.style).lower()
                if amber_lower in style_str:
                    found = True
                    break
        assert found, (
            f"agent label 'reyn' must use _AMBER ({_AMBER}); "
            f"got segments={[(s.text, str(s.style)) for s in strip]}"
        )


@pytest.mark.asyncio
async def test_streaming_cursor_uses_amber() -> None:
    """The streaming cursor ▍ (= agent producing) uses _AMBER, not _CORAL.

    The cursor is an agent-identity signal — it tells the user the
    agent is mid-utterance. Pinning it to amber keeps the agent-identity
    family consistent (header + cursor read as one signal).
    """
    from reyn.chat.tui.widgets.streaming_row import StreamingRow
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        row = conv.begin_stream("test_stream_amber", "reyn")
        row.append("partial")
        await pilot.pause()

        rendered = row._build_renderable()
        amber_lower = _AMBER.lower()
        coral_lower = _CORAL.lower()
        # Walk every span; find ones carrying the cursor or the prefix.
        styles_used = []
        for span in rendered.spans:
            style_str = str(span.style).lower()
            styles_used.append(style_str)
        joined = " | ".join(styles_used)
        assert amber_lower in joined, (
            f"streaming row styles must include _AMBER ({_AMBER}); got {joined}"
        )
        assert coral_lower not in joined, (
            f"streaming row must NOT use _CORAL (= action colour); got {joined}"
        )


# ── action sites stay on _CORAL ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fold_hint_uses_coral_action_colour() -> None:
    """The ``/expand`` fold hint stays on _CORAL — it's an action affordance.

    Defence against an over-eager refactor that swept every coral to
    amber: the action / cursor channel must keep its distinct hue, or
    the semantic split collapses back to a single accent.
    """
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Trigger fold with a long reply (> _FOLD_THRESHOLD_LINES)
        long_text = "\n".join(f"line {i}" for i in range(60))
        conv._write_agent_markdown_with_fold(long_text)
        await pilot.pause()

        # Find the fold hint strip
        hint_strip = None
        for s in log.lines:
            text = "".join(seg.text for seg in s)
            if "/expand" in text or "more lines" in text:
                hint_strip = s
                break
        assert hint_strip is not None, "fold hint strip not found"

        coral_lower = _CORAL.lower()
        amber_lower = _AMBER.lower()
        any_coral = any(
            coral_lower in (str(seg.style).lower() if seg.style else "")
            for seg in hint_strip
        )
        any_amber = any(
            amber_lower in (str(seg.style).lower() if seg.style else "")
            for seg in hint_strip
        )
        assert any_coral, (
            f"fold hint must use _CORAL ({_CORAL}); "
            f"got segs={[(s.text, str(s.style)) for s in hint_strip]}"
        )
        assert not any_amber, (
            f"fold hint must NOT use _AMBER (= agent identity); "
            f"got segs={[(s.text, str(s.style)) for s in hint_strip]}"
        )
