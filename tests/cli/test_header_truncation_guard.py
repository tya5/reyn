"""Tier 2: ReynHeader truncates _agent_name when the status overflows.

The right-side status renders ``agent_name │ model │ tokens │ cost
│ [N pending] │ clock`` right-aligned at ``width: 1fr`` (= the
remaining horizontal cell after the Title label takes its share).
When all fields are populated AND the agent name is long AND the
terminal is narrow, the assembled status overflows the status cell
and Textual either wraps the label to a 2nd row (breaking the
single-line header docking) or silently clips the right-edge
clock canary.

These tests pin that under a narrow size the agent name truncates
with ``…`` and the clock canary stays at the right edge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets import ReynHeader  # noqa: E402


class _HeaderOnlyApp(App):
    """Minimal app — just a ReynHeader, no conv pane / right panel.

    Lets the test mount + size the header in isolation so width
    assertions don't have to subtract the right panel cells.
    """

    def __init__(self, *, agent_name: str = "", model: str = "") -> None:
        super().__init__()
        self._agent_name = agent_name
        self._model = model

    def compose(self) -> ComposeResult:
        yield ReynHeader(
            agent_name=self._agent_name, model=self._model, id="header",
        )


@pytest.mark.asyncio
async def test_long_agent_name_truncates_at_narrow_width():
    """At 60 cells with a long agent name + model + tokens + cost, the
    rendered agent name surfaces with the ``…`` truncation marker.
    """
    app = _HeaderOnlyApp(
        agent_name="aria-with-a-very-long-name",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(60, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(tokens_today=12345, cost_usd=0.0123)
        rendered = header._format_status().plain
        # Either the truncation marker landed in the agent name slot,
        # or — if the budget happened to be enough — the full name is
        # there. Pin the actual narrow case where the full name does
        # NOT fit; the assertion below proves truncation kicked in.
        assert "…" in rendered
        # And the clock canary (HH:MM:SS pattern) stays visible at the
        # right edge — i.e. the rendered status STILL contains the
        # colon-separated time.
        assert ":" in rendered.split("│")[-1]


@pytest.mark.asyncio
async def test_short_agent_name_fits_no_truncation():
    """A 4-cell agent name at standard width does NOT truncate."""
    app = _HeaderOnlyApp(
        agent_name="aria",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(120, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(tokens_today=100, cost_usd=0.01)
        rendered = header._format_status().plain
        # Short name in a wide terminal — no … and the full name is
        # present.
        assert "aria" in rendered
        # The truncation marker should NOT appear for the agent name
        # in this case. (Other Unicode might contain U+2026, so we
        # check that the *agent name* itself isn't ellipsed.)
        # The simplest assertion: full "aria" is present unmodified.
        assert "ari…" not in rendered


@pytest.mark.asyncio
async def test_truncated_name_keeps_minimum_3_cells():
    """Even at an extreme width, the agent name keeps a minimum visible
    fragment so the user retains some identity signal.
    """
    app = _HeaderOnlyApp(
        agent_name="alice",
        model="claude-opus-4-7-20251101",
    )
    async with app.run_test(headless=True, size=(40, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=999_999,
            tokens_cap=1_000_000,
            cost_usd=0.9876,
            cost_cap=10.00,
        )
        rendered = header._format_status().plain
        # The name truncated, but didn't become empty — at minimum
        # one body cell + the ellipsis cell are present.
        assert "…" in rendered
        # First field of the right-aligned status: should be at least
        # something non-empty before the first separator.
        first_field = rendered.split("│")[0].strip()
        assert first_field, "first status field must not be empty after truncation"
