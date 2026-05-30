"""Tier 2: FoldableMarkdown widget — bidirectional toggle for long replies.

Tests pin the contract introduced by the B3 toggle refactor:
  - FoldableMarkdown renders preview + ▶ hint in collapsed state.
  - toggle() switches to full text + ▼ hint.
  - toggle() twice returns to preview (= idempotent bidirectional).
  - is_expanded() accurately reflects toggle state.
  - ConversationView mounts FoldableMarkdown for long replies.
  - toggle_last_foldable() toggles latest widget, returns True.
  - toggle_last_foldable() with no foldables returns False.
  - clicking hint bar (Label.fm-hint) triggers toggle; body click does not.
  - /expand slash path (= _on_expand_last_reply) calls toggle_last_foldable().
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_app():
    from reyn.chat.tui.app import ReynTUIApp
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _long_reply(n: int = 60) -> str:
    return "\n".join(f"line {i}" for i in range(n))


# ── 1. Collapsed state renders preview + ▶ hint ──────────────────────────────

@pytest.mark.asyncio
async def test_foldable_collapsed_shows_preview_and_glyph() -> None:
    """Tier 2: collapsed FoldableMarkdown has preview text and ▶ hint.

    Checks _remaining_lines is stored and _collapsed_hint() contains
    the ▶ glyph and "more lines" — both are required by the UX spec.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        foldables = list(conv.query(FoldableMarkdown))
        assert foldables, "FoldableMarkdown should be mounted for long reply"
        fm = foldables[-1]
        assert not fm.is_expanded(), "widget starts collapsed"
        assert fm.remaining_lines == 30, (
            f"remaining_lines should be 30 (60 lines - 30 threshold), got {fm.remaining_lines}"
        )
        hint = fm._collapsed_hint()
        assert "▶" in hint, f"collapsed hint should contain ▶, got: {hint!r}"
        assert "more lines" in hint, f"collapsed hint should contain 'more lines', got: {hint!r}"
        # Preview text should be first 30 lines
        assert "line 0" in fm.preview_text
        # Full text should contain all lines
        assert "line 59" in fm.full_text


# ── 2. After toggle() → full text + ▼ hint ───────────────────────────────────

@pytest.mark.asyncio
async def test_foldable_toggle_shows_full_text_and_collapse_glyph() -> None:
    """Tier 2: after toggle() the widget shows full text and ▼ hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = list(conv.query(FoldableMarkdown))[-1]
        fm.toggle()
        await pilot.pause()

        assert fm.is_expanded(), "widget should be expanded after toggle()"
        hint = fm._expanded_hint()
        assert "▼" in hint, f"expanded hint should contain ▼, got: {hint!r}"
        assert "collapse" in hint, f"expanded hint should contain 'collapse', got: {hint!r}"


# ── 3. toggle() twice → back to preview (idempotent) ─────────────────────────

@pytest.mark.asyncio
async def test_foldable_double_toggle_returns_to_preview() -> None:
    """Tier 2: two toggle() calls cycle back to collapsed (bidirectional)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = list(conv.query(FoldableMarkdown))[-1]
        assert not fm.is_expanded()

        fm.toggle()
        await pilot.pause()
        assert fm.is_expanded(), "after 1st toggle: expanded"

        fm.toggle()
        await pilot.pause()
        assert not fm.is_expanded(), "after 2nd toggle: collapsed again"


# ── 4. is_expanded() tracks toggle accurately ────────────────────────────────

@pytest.mark.asyncio
async def test_foldable_is_expanded_tracks_state() -> None:
    """Tier 2: is_expanded() accurately reflects state across multiple toggles."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = list(conv.query(FoldableMarkdown))[-1]
        states = []
        for _ in range(4):
            states.append(fm.is_expanded())
            fm.toggle()
            await pilot.pause()
        # Starting collapsed: False, True, False, True
        assert states == [False, True, False, True], (
            f"is_expanded() should cycle F/T/F/T, got {states}"
        )


# ── 5. ConversationView._foldables has 1 widget after long reply ──────────────

@pytest.mark.asyncio
async def test_conv_foldables_list_has_one_after_long_reply() -> None:
    """Tier 2: _foldables list has exactly 1 widget after one long reply."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert not conv.foldables, "starts empty"
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()
        (only_foldable,) = conv.foldables


# ── 6. toggle_last_foldable() toggles latest and returns True ────────────────

@pytest.mark.asyncio
async def test_toggle_last_foldable_returns_true_and_toggles() -> None:
    """Tier 2: toggle_last_foldable() returns True and toggles latest widget."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = conv.foldables[-1]
        assert not fm.is_expanded()

        result = conv.toggle_last_foldable()
        assert result is True, "toggle_last_foldable() should return True"
        await pilot.pause()
        assert fm.is_expanded(), "widget should be expanded after toggle_last_foldable()"

        # Second call toggles back
        result2 = conv.toggle_last_foldable()
        assert result2 is True
        await pilot.pause()
        assert not fm.is_expanded(), "second toggle_last_foldable() collapses"


# ── 7. toggle_last_foldable() with no foldables returns False ─────────────────

@pytest.mark.asyncio
async def test_toggle_last_foldable_returns_false_when_none() -> None:
    """Tier 2: toggle_last_foldable() returns False when no foldables exist."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        result = conv.toggle_last_foldable()
        assert result is False, "returns False when no foldables"


# ── 8. hint-bar click triggers toggle; body click does not ───────────────────

@pytest.mark.asyncio
async def test_foldable_hint_click_triggers_toggle() -> None:
    """Tier 2: clicking the fm-hint Label toggles is_expanded (C4 scoped click).

    Exercises the full Textual pilot.click path so event.widget routing
    is exercised: clicking the hint bar expands/collapses; clicking the
    body Static leaves the state unchanged.
    """
    from textual.widgets import Label, Static

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = list(conv.query(FoldableMarkdown))[-1]
        hint = fm.query_one(".fm-hint", Label)
        body = fm.query_one(".fm-body", Static)
        assert not fm.is_expanded()

        # Click hint bar → expands
        await pilot.click(hint)
        await pilot.pause()
        assert fm.is_expanded(), "clicking hint bar should expand the widget"

        # Click hint bar again → collapses
        await pilot.click(hint)
        await pilot.pause()
        assert not fm.is_expanded(), "second hint click should collapse"

        # Click the body Static → state unchanged (body click does not toggle)
        await pilot.click(body)
        await pilot.pause()
        assert not fm.is_expanded(), "clicking body should NOT toggle expanded state"


# ── 9. /expand path calls toggle_last_foldable ───────────────────────────────

@pytest.mark.asyncio
async def test_expand_slash_path_toggles_foldable() -> None:
    """Tier 2: _on_expand_last_reply calls toggle_last_foldable() (integration).

    Verifies that the /expand handler path (= _on_expand_last_reply in
    app_outbox.py) calls conv.toggle_last_foldable(), which in turn
    toggles the latest FoldableMarkdown widget.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()

        fm = list(conv.query(FoldableMarkdown))[-1]
        assert not fm.is_expanded()

        # Invoke toggle_last_foldable (= what _on_expand_last_reply calls)
        toggled = conv.toggle_last_foldable()
        assert toggled is True
        await pilot.pause()
        assert fm.is_expanded(), "widget expanded via toggle_last_foldable (= /expand path)"
