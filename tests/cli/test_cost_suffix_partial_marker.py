"""Tier 2: ``render_cost_suffix`` marks the partial-mid-skill case with ``~``.

Wave-6 ST3 introduced a deferral loop in ``ReynTUIApp._maybe_render_cost_suffix``
that retries up to ``_COST_SUFFIX_DEFER_MAX_ATTEMPTS`` (= 30 × 1 s) while
a skill is still in ``_skill_exec``. When the cap fires the line is
emitted anyway with whatever the budget tracker snapshot shows — which
under-reports the eventual total if the skill is still spending tokens.
Without a visual marker, the user reads the line as if it were the
final number.

These tests pin the wave-7 C-F6 contract: when ``partial=True`` is
passed, each numeric segment is prefixed with ``~`` and a
``"(skill still running)"`` suffix is appended, so the partial nature
is visible at a glance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets import ConversationView  # noqa: E402


class _ConvOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield ConversationView(id="conversation")


def _last_rendered_text(conv: ConversationView) -> str:
    """Return the plain-text content of the last line written to the RichLog."""
    from textual.widgets import RichLog
    log = conv.query_one(RichLog)
    if not log.lines:
        return ""
    return log.lines[-1].text


@pytest.mark.asyncio
async def test_cost_suffix_partial_prefixes_numbers_with_tilde():
    """Tier 2b: ``partial=True`` → all three numeric segments carry the ``~`` prefix."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.render_cost_suffix(
            tokens=12345, cost_usd=0.0123, elapsed_s=8.5, partial=True,
        )
        await pilot.pause()
        rendered = _last_rendered_text(conv)
        assert "~12345t" in rendered
        assert "~$0.0123" in rendered
        assert "~8.5s" in rendered
        assert "skill still running" in rendered


@pytest.mark.asyncio
async def test_cost_suffix_default_has_no_tilde():
    """Tier 2b: Default (``partial=False``) emits the unchanged shape."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.render_cost_suffix(
            tokens=12345, cost_usd=0.0123, elapsed_s=8.5,
        )
        await pilot.pause()
        rendered = _last_rendered_text(conv)
        assert "12345t" in rendered
        assert "$0.0123" in rendered
        assert "8.5s" in rendered
        # No tilde prefix on the numeric fields, no skill-still-running marker.
        assert "~" not in rendered
        assert "skill still running" not in rendered
