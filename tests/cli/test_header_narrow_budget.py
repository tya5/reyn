"""Tier 2: ReynHeader voice + find badge cells included in truncation budget.

Wave-13 narrow-terminal regression. Bug: ``_maybe_truncate_agent_name``
did not account for the voice and ``/find`` badge cells in ``other_cells``
or ``part_count``. With voice recording active, the name was truncated
by a smaller amount (voice cells not counted), causing the assembled
status to overflow → Label height=2 → clock canary + voice badge silently
clipped by the CSS ``height: 1``.

Fix: voice badge cells and find badge cells are now added to ``other_cells``
with the exact same text as ``_format_status`` emits, and the matching +1
entries are added to ``part_count``.

What these tests pin:
  - At a width where the budget is positive (≥ 3 cells for the name),
    the assembled status fits in height=1 even with voice recording + caps.
  - The agent name is truncated with ``…`` when voice recording is active.
  - The clock canary stays at the right edge after truncation.
  - The find badge is also accounted for when active.

Note on width selection: the recording badge alone is 32 cells
(``🔴 voice · Enter→send Esc→cancel``). At 80 cols the fixed fields
(model+tok_cap+cost_cap+clock = 58 cells) plus 5 separators (25 cells)
plus the voice badge (32 cells) total 115 cells against 72 available —
a deficit that no amount of name truncation can fix. Tests therefore
use 140-col terminals where the budget is positive and truncation is
meaningful.

Tier self-check:
  - No MagicMock / AsyncMock / patch
  - Docstrings declare Tier 2b
  - No private-state assertions except the public ``.size`` surface
  - No snapshot / golden-file output
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.widgets import ReynHeader  # noqa: E402


class _HeaderOnlyApp(App):
    """Minimal app — just a ReynHeader, no conv pane / right panel."""

    def __init__(self, *, agent_name: str = "", model: str = "") -> None:
        super().__init__()
        self._agent_name = agent_name
        self._model = model

    def compose(self) -> ComposeResult:
        yield ReynHeader(
            agent_name=self._agent_name, model=self._model, id="header",
        )


@pytest.mark.asyncio
async def test_140col_budget_caps_no_voice_fits_one_row() -> None:
    """Tier 2b: 140-col + budget caps + no voice badge → status label height == 1.

    Baseline: verifies that without any active badges the header fits at
    140 columns — the budget is positive and truncation keeps the label
    in one row.
    """
    app = _HeaderOnlyApp(
        agent_name="aria-with-a-very-long-agent-name",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(140, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.0,
        )
        await pilot.pause()
        label = header.query_one("#status")
        assert label.size.height == 1, (
            f"Label height={label.size.height} at 140-col + budget caps + no voice "
            f"— truncation not keeping status in one row"
        )


@pytest.mark.asyncio
async def test_140col_budget_caps_voice_recording_fits_one_row() -> None:
    """Tier 2b: 140-col + budget caps + voice recording → status label height == 1.

    This is the primary regression test for the wave-13 bug. At 140 cols
    the budget for the agent name (after accounting for all fixed fields)
    is positive. Without the fix, voice badge cells were absent from the
    ``other_cells`` budget, so the name was truncated by too little and
    the assembled status overflowed → height=2.

    Post-fix: voice cells are included, name truncates to the correct
    smaller budget, assembled text fits in one row.
    """
    app = _HeaderOnlyApp(
        agent_name="aria-with-a-very-long-agent-name",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(140, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.0,
        )
        header.set_voice_state("recording")
        await pilot.pause()
        label = header.query_one("#status")
        assert label.size.height == 1, (
            f"Label height={label.size.height} at 140-col + caps + voice recording "
            f"— voice badge cells missing from other_cells budget (pre-fix regression)"
        )


@pytest.mark.asyncio
async def test_140col_budget_caps_find_badge_fits_one_row() -> None:
    """Tier 2b: 140-col + budget caps + find badge active → status label height == 1."""
    app = _HeaderOnlyApp(
        agent_name="aria-with-a-very-long-agent-name",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(140, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.0,
        )
        header.set_find_state("query", position=2, total=5)
        await pilot.pause()
        label = header.query_one("#status")
        assert label.size.height == 1, (
            f"Label height={label.size.height} at 140-col + caps + find badge "
            f"— find badge cells missing from other_cells budget (pre-fix regression)"
        )


@pytest.mark.asyncio
async def test_agent_name_truncated_to_ellipsis_with_voice_and_caps() -> None:
    """Tier 2b: voice recording + caps forces the agent name to truncate with '…'.

    Validates that the fix path actually truncates (= the budget is tight
    enough at 140 cols that the 32-cell name cannot fit alongside voice
    recording + caps + clock). Clock canary must stay at the right edge.
    """
    app = _HeaderOnlyApp(
        agent_name="aria-with-a-very-long-agent-name",
        model="claude-opus-4-7",
    )
    async with app.run_test(headless=True, size=(140, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.0,
        )
        header.set_voice_state("recording")
        await pilot.pause()
        rendered = header._format_status().plain
        # At 140 cols with voice recording badge + caps the agent name
        # must be truncated (32 cells > budget of ~17 cells).
        assert "…" in rendered, (
            f"Expected agent name truncation at 140-col + voice recording, "
            f"rendered={rendered!r}"
        )
        # Clock canary must still be visible at the right edge.
        assert ":" in rendered.split("│")[-1], (
            f"Clock canary disappeared from right edge — rendered={rendered!r}"
        )
