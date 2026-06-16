"""Tier 2: ReasoningBlock — collapsible reasoning-text render (#1652).

Pins the widget's public render surface (``render_header`` / ``render_body`` →
Rich ``Text.plain``) + the collapse contract: default expanded (reasoning shown),
body hidden when collapsed, click toggles. Render-only widget — no outbox wiring
here (that seam mounts ReasoningBlock when an agent message carries non-empty
``reasoning``; finalised against e2e's #1652 outbox struct).

Asserts on the public surface, never private state (per CLAUDE.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.reasoning_block import ReasoningBlock  # noqa: E402


def test_default_expanded_shows_full_reasoning_text() -> None:
    """Tier 2: default expanded — header marks it as reasoning + body has the text."""
    block = ReasoningBlock(reasoning="step one\nstep two\nstep three")
    assert block.is_expanded is True                       # #1652 contract: shown by default
    header = block.render_header().plain
    assert "reasoning" in header
    assert "3 lines" in header                              # line count surfaced
    assert "▾" in header                                    # expanded glyph
    body = block.render_body().plain
    assert "step one" in body and "step three" in body      # full text rendered


def test_collapsed_hides_body_keeps_header() -> None:
    """Tier 2: collapsing hides the body text but keeps the header (+ ▸ glyph)."""
    block = ReasoningBlock(reasoning="secret thoughts here")
    block.toggle_expand()
    assert block.is_expanded is False
    assert block.render_body().plain == ""                  # body hidden when collapsed
    header = block.render_header().plain
    assert "reasoning" in header
    assert "▸" in header                                    # collapsed glyph
    assert "secret thoughts here" not in header             # text not leaked into header


def test_singular_line_count_label() -> None:
    """Tier 2: single-line reasoning reads '1 line' (not '1 lines')."""
    assert "1 line" in ReasoningBlock(reasoning="just one").render_header().plain
    assert "1 lines" not in ReasoningBlock(reasoning="just one").render_header().plain


def test_empty_reasoning_renders_no_body() -> None:
    """Tier 2: empty reasoning → 0 lines, empty body (defensive — block shouldn't
    be mounted at all for empty reasoning, but render must not error)."""
    block = ReasoningBlock(reasoning="")
    assert "0 line" in block.render_header().plain
    assert block.render_body().plain == ""


class _BlockApp(App):
    def compose(self) -> ComposeResult:
        yield ReasoningBlock(reasoning="a\nb", id="rb")


@pytest.mark.asyncio
async def test_click_toggles_collapse() -> None:
    """Tier 2: a mouse click on the mounted block flips collapse (mirrors the
    ToolCallRow / SkillActivityRow click contract). run_test covers the
    mouse-driven path (no key binding involved)."""
    app = _BlockApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        block = app.query_one("#rb", ReasoningBlock)
        assert block.is_expanded is True
        await pilot.click("#rb")
        await pilot.pause()
        assert block.is_expanded is False                   # click collapsed it
        await pilot.click("#rb")
        await pilot.pause()
        assert block.is_expanded is True                    # click expanded it back
