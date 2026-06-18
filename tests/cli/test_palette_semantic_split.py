"""Tier 2: palette splits coral into action-affordance vs amber agent-identity.

Visual UX audit (HIGH severity Finding F1): a single ``_CORAL`` accent
was used for agent identity (header label, streaming cursor,
intervention prefix), interactive affordances (picker selection caret,
panel cursor ``▶``), and status glyphs. The eye saw coral everywhere
with no semantic rule.

The split:

  • ``_AMBER`` = agent identity (header label, streaming cursor ``▍``,
    intervention "Aria asks" prefix). One read: "this names the agent".
  • ``_CORAL`` = interactive affordance / "you are here" (picker
    selection caret, panel cursor ``▶``).
    One read: "you can act here / your cursor is here".

These tests pin both reads — palette values exist and are distinct, and
the load-bearing render paths (agent header, streaming cursor) use the
right one of the two. Note: fold/expand machinery removed; conversation
replies render full inline (Claude-Code-style).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.color import Color as _RichColor

from reyn.interfaces.tui._palette import _AMBER, _CORAL
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView
from reyn.runtime.outbox import OutboxMessage


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── palette: two distinct named colours ──────────────────────────────────────


def test_amber_and_coral_are_distinct() -> None:
    """Tier 2b: palette declares both names and they must not collide.

    A drift back to a single accent (``_AMBER = _CORAL``) would silently
    undo the entire semantic split, so pin the inequality.
    """
    assert _AMBER != _CORAL, (
        f"_AMBER and _CORAL must be distinct hex strings; got both = {_AMBER!r}"
    )
    # Both must be valid hex colour strings (prefix check only — not size/shape)
    assert _AMBER.startswith("#")
    assert _CORAL.startswith("#")


def test_amber_is_in_warm_hue_family() -> None:
    """Tier 2b: amber should sit in a warm hue family (R dominant, B muted).

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
    """Tier 2b: the agent header label ``reyn`` carries the _AMBER colour.

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

        # Render an agent message so the agent symbol appears in a header.
        # Post-refactor: headers are symbol-only (``⏺``); no label text or dash rule.
        from reyn.interfaces.tui.widgets.conversation import _GLYPH_AGENT
        conv.render_message(OutboxMessage(kind="agent", text="hi"))
        await pilot.pause()

        # Find the strip containing the agent symbol (= the header line).
        strip = None
        for s in log.lines:
            text = "".join(seg.text for seg in s)
            if _GLYPH_AGENT in text:
                strip = s
                break
        assert strip is not None, "agent header strip not found"

        # The segment containing the agent symbol must carry the amber colour.
        amber_lower = _AMBER.lower()
        found = False
        for seg in strip:
            if _GLYPH_AGENT in seg.text and seg.style is not None:
                style_str = str(seg.style).lower()
                if amber_lower in style_str:
                    found = True
                    break
        assert found, (
            f"agent symbol {_GLYPH_AGENT!r} must use _AMBER ({_AMBER}); "
            f"got segments={[(s.text, str(s.style)) for s in strip]}"
        )


@pytest.mark.asyncio
async def test_streaming_cursor_uses_amber() -> None:
    """Tier 2b: the streaming cursor ▍ (= agent producing) uses _AMBER, not _CORAL.

    The cursor is an agent-identity signal — it tells the user the
    agent is mid-utterance. Pinning it to amber keeps the agent-identity
    family consistent (header + cursor read as one signal).
    """
    from reyn.interfaces.tui.widgets.streaming_row import StreamingRow
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
async def test_long_reply_renders_full_inline_without_amber() -> None:
    """Tier 2b: long agent replies render full inline (no fold); amber not used in body.

    Fold/expand machinery removed per user direction. Replies of any length
    render directly into the RichLog via _write_agent_markdown. This test
    verifies:
      1. A long reply (60 lines) is accepted by _write_agent_markdown and
         rendered fully inline (no collapse widget mounted).
      2. The palette invariant still holds: reply body text in the RichLog
         does NOT carry _AMBER styling (agent identity colour stays on the
         header symbol only, not body text).
    """
    from textual.widgets import RichLog

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        lines_before = len(list(getattr(log, "lines", [])))

        # Send a long reply (60 lines) — should render full inline, no fold widget.
        long_text = "\n".join(f"line {i}" for i in range(60))
        conv._write_agent_markdown(long_text)
        await pilot.pause()

        # Fold removed: no collapse widget mounted; reply goes straight into RichLog.
        lines_after = len(list(getattr(log, "lines", [])))
        assert lines_after > lines_before, (
            "Long reply must write at least one line into RichLog"
        )

        # Palette check: body content must not carry _AMBER (amber = agent identity).
        # We look for strips added after lines_before; none should use the amber colour.
        amber_lower = _AMBER.lower()
        body_strips = list(getattr(log, "lines", []))[lines_before:]
        for strip in body_strips:
            for seg in strip:
                if seg.text.strip():
                    style_str = str(seg.style).lower() if seg.style else ""
                    assert amber_lower not in style_str, (
                        f"Body segment {seg.text!r} must not use _AMBER in a reply body; "
                        f"got style={style_str}"
                    )
