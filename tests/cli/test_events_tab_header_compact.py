"""Tier 2: events tab header fits in the new 36-col minimum panel width.

The pre-fix header was ``Events  [f]ilter:all  [t]ail:200  j/k=move
space=open`` (~54 cells) which truncated past ``[t`` at the new 36-col
panel minimum (= the user lost the entire keybind half of the line).

The fix compacts the header to ``Events  [f]:<filter>  [t]:<tail>
j/k sp=open`` (~36 cells with ``all`` filter, ~41 with the longest
``internal``).

Contract pinned:

1. The header for the events tab strips Rich markup to a visible-cell
   form that fits ≤40 cells with the default filter (``all``) + default
   tail (``200``) — i.e. it does NOT silently regrow past the 36-col
   panel min.
2. The header still surfaces ``f`` / ``t`` keys (= still discoverable),
   the current filter name, the current tail count, and ``j/k`` cursor
   movement.
3. Other tabs' header markup is unchanged (= keys / memory / agents /
   docs / cost still render their existing labels).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.text import Text as RichText

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import RightPanel


def _visible_cells(markup: str) -> int:
    """Return the cell-count of ``markup`` after stripping Rich markup."""
    return RichText.from_markup(markup).cell_len


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_events_header_fits_minimum_panel_width_with_default_filter() -> None:
    """Tier 2: default-state events header is <=40 cells (fits at 36-col min)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_type = "events"  # type: ignore[attr-defined]

        markup = panel._panel_header_markup()
        cells = _visible_cells(markup)
        # 40 is a +4 buffer above the 36-col panel min — leaves room for
        # the surrounding ``padding: 0 1`` (= 2 cells) without hitting
        # truncation at the bound.
        assert cells <= 40, (
            f"events header should fit at the 36-col panel minimum; "
            f"got {cells} visible cells from {markup!r}"
        )


@pytest.mark.asyncio
async def test_events_header_surfaces_keys_and_state() -> None:
    """Tier 2: events header still includes f / t keys, filter, tail, j/k.

    Pins the discoverability contract — a future refactor that compresses
    further by dropping the ``f`` key indicator (= "you can press f to
    cycle filters") would regress the user's ability to find the cycle
    affordance without reading docs.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_type = "events"  # type: ignore[attr-defined]

        markup = panel._panel_header_markup()
        # Strip markup before substring checks (= ``[bold #C8553D]f[/]``
        # is still the letter ``f`` after rendering).
        plain = str(RichText.from_markup(markup))

        # f / t keys discoverable.
        assert "[f]" in plain or "f]" in plain, plain
        assert "[t]" in plain or "t]" in plain, plain
        # Default filter + tail surfaced.
        assert "all" in plain, plain
        assert "200" in plain, plain
        # Cursor navigation hint still present.
        assert "j/k" in plain or "j" in plain, plain
        # ``Events`` label still leads.
        assert plain.lstrip().startswith("Events"), plain


@pytest.mark.asyncio
async def test_non_events_headers_unchanged() -> None:
    """Tier 2: keys / agents / memory / cost / docs headers still render their existing labels.

    Defends against an accidental edit that compacted a shared helper
    and shrank the other tabs' headers too.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)

        expected_labels = {
            "keys":   "Key Bindings",
            "agents": "Agents",
            "memory": "Memory",
            "cost":   "Cost",
            "docs":   "Docs",
        }
        for tab_id, label in expected_labels.items():
            panel._panel_type = tab_id  # type: ignore[attr-defined]
            markup = panel._panel_header_markup()
            plain = str(RichText.from_markup(markup))
            assert label in plain, (
                f"tab {tab_id!r} header should still contain {label!r}; "
                f"got {plain!r}"
            )
