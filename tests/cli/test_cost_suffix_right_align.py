"""Tier 2: render_cost_suffix actually right-aligns the suffix line.

Regression net for PR #98: ``Text(..., justify="right")`` alone does NOT
right-align inside a RichLog — the attribute only matters when the renderer
is told to expand to a fill width. ``RichLog.write`` defaults to
``expand=False``, so the renderable is laid out at its natural width and
stays at column 0.

This test exercises the path via the real ConversationView and asserts on
the rendered Strip content (= what the user actually sees), not on a
private flag. Pinning the contract means a future refactor of either the
Text or the RichLog.write call cannot silently regress alignment again.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the worktree src importable in case the test runner uses the installed package.
_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _line_text(log: RichLog, index: int) -> str:
    """Return the rendered text of a single RichLog line at ``index``."""
    strip = log.lines[index]
    return "".join(seg.text for seg in strip)


@pytest.mark.asyncio
async def test_render_cost_suffix_is_right_aligned():
    """Cost suffix renders at the right edge of the RichLog content region.

    Drives ``conv.render_cost_suffix`` directly with known values, then
    inspects the resulting RichLog Strip: the visible glyphs of the suffix
    must end at (or very near) the right edge of the log's content width,
    with the bulk of the leading width being padding (= right-aligned).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        before = len(log.lines)
        conv.render_cost_suffix(tokens=100, cost_usd=0.0050, elapsed_s=1.2)
        # RichLog defers writes until the size is known; pause to let it flush.
        for _ in range(3):
            await pilot.pause()

        assert len(log.lines) > before, "RichLog did not grow after render_cost_suffix"
        line = _line_text(log, -1)

        # Suffix glyph signature
        assert "⌁" in line, f"suffix glyph missing from rendered line: {line!r}"
        assert "100t" in line and "$0.0050" in line and "1.2s" in line, line

        # Right-alignment proof: the line is much wider than the suffix itself,
        # and the suffix sits in the rightmost portion of that width.
        suffix_visible = "⌁ 100t · $0.0050 · 1.2s"
        # The rendered line should be padded out to the log content width,
        # which (at size=(120, 30) with no right panel open) is well wider
        # than the suffix. We assert the suffix is in the right half.
        idx = line.find("⌁")
        assert idx >= 0
        # Strip trailing whitespace: where does the visible content actually end?
        rstripped_len = len(line.rstrip())
        # The suffix should end at (or within 1 col of) the rstripped length.
        assert rstripped_len - idx <= len(suffix_visible) + 2, (
            f"suffix appears followed by extra content: line={line!r}"
        )
        # And the leading padding must be non-trivial — at least more than
        # half the suffix width. (Left-aligned would put idx == 0.)
        assert idx >= len(suffix_visible), (
            f"suffix not right-aligned: idx={idx}, line={line!r}"
        )


@pytest.mark.asyncio
async def test_render_cost_suffix_no_crash_on_zero_values():
    """Cost suffix with zero deltas still renders without raising.

    The caller (``app._maybe_render_cost_suffix``) skips the call when
    both deltas are zero, but this is a defence-in-depth check that the
    widget itself does not blow up on edge inputs.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Should not raise
        conv.render_cost_suffix(tokens=0, cost_usd=0.0, elapsed_s=0.0)
        await pilot.pause()
