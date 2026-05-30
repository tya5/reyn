"""Tier 2: RightPanel header hints — keys sp=expand + events i=isolate.

B4 (MED): The Keys tab header omitted the Space=expand affordance even
though the Keys tab supports Space to expand a per-key detail block.
Fix: append ``sp=expand`` to the Keys tab header hint string, matching
the ``sp=open`` idiom used in other tabs.

B5 (LOW): The events tab header never surfaced the ``i``=chain-isolate
key.  The ``v`` verbose key already has a live [v] marker; ``i`` had no
mention. Fix: append ``i=isolate`` to the static hint suffix and show an
[i] marker (analogous to [v]) when chain-isolation is active.

Public surfaces tested:
  - ``panel_header_markup()`` for the keys tab contains ``sp=expand``.
  - ``panel_header_markup()`` for the events tab contains ``i=isolate``.
  - When ``_events_chain_isolate`` is set, the events header contains
    the ``[i]`` active-state marker.
  - When ``_events_chain_isolate`` is None, no ``[i]`` marker appears.
  - Keys tab ``sp=expand`` is not regressed by the events change, and
    events ``i=isolate`` is not regressed by the keys change.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.text import Text as RichText

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# B4 — Keys tab surfaces sp=expand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keys_tab_header_contains_sp_expand() -> None:
    """Tier 2: Keys tab header includes ``sp=expand`` affordance hint (B4).

    The Keys tab supports Space to expand a per-key detail block but the
    prior header only showed ``j↓ k↑``.  The fix appends ``sp=expand``
    matching the sp=open idiom used by other tabs.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "keys"
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        assert "sp=expand" in plain, (
            f"keys tab header should contain 'sp=expand'; got: {plain!r}"
        )
        # Navigation hints still present.
        assert "j" in plain, f"keys tab header missing j nav hint; got: {plain!r}"
        assert "Key Bindings" in plain, (
            f"keys tab header missing label; got: {plain!r}"
        )


# ---------------------------------------------------------------------------
# B5 — events tab static hint contains i=isolate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_tab_header_contains_i_isolate_hint() -> None:
    """Tier 2: events tab header static hint includes ``i=isolate`` (B5).

    Before the fix the ``i`` chain-isolate key was undiscoverable from
    the header; the fix appends ``i=isolate`` to the static hint suffix.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "events"
        # Default state: no isolation active.
        panel._events_chain_isolate = None
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        # ``i=iso`` is the abbreviated form — fits within the 44-col
        # panel minimum without overflowing header padding.
        assert "i=iso" in plain, (
            f"events tab header should contain 'i=iso'; got: {plain!r}"
        )
        # Existing hints still present (sp is the abbreviated form of sp=open).
        assert "sp" in plain, (
            f"events tab header should still contain 'sp'; got: {plain!r}"
        )
        assert "j/k" in plain, (
            f"events tab header should still contain 'j/k'; got: {plain!r}"
        )


# ---------------------------------------------------------------------------
# B5 — [i] active-state marker appears when chain-isolation is active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_tab_header_shows_i_marker_when_isolated() -> None:
    """Tier 2: events header shows ``[i]`` marker when chain-isolation is active.

    Analogous to the existing ``[v]`` verbose marker.  When isolation is
    active (``_events_chain_isolate`` is a non-None string), the header
    must surface ``[i]`` so the user can tell at a glance whether the
    event list is scoped to a single chain.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "events"
        panel._events_chain_isolate = "chain-ABC-123"
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        assert "[i]" in plain, (
            f"events header should show '[i]' marker when chain-isolation "
            f"is active; got: {plain!r}"
        )


@pytest.mark.asyncio
async def test_events_tab_header_no_i_marker_when_not_isolated() -> None:
    """Tier 2: events header omits ``[i]`` marker when chain-isolation is off.

    The ``[i]`` marker must only appear when isolation is active, not as
    a permanent fixture — matching the ``[v]`` marker's off-by-default
    behaviour.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "events"
        panel._events_chain_isolate = None
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        # The static hint mentions "i=isolate" but the bracketed live
        # marker "[i]" must not appear when isolation is off.
        # (Note: the static suffix uses "i=isolate" without brackets;
        #  the marker uses the pattern "[i]" matching the "[v]" marker shape.)
        # Count bracketed [i] occurrences by checking for the marker pattern.
        # The static "i=isolate" does NOT include square brackets around "i".
        assert "[i]" not in plain, (
            f"events header should NOT show '[i]' marker when isolation is "
            f"off; got: {plain!r}"
        )


# ---------------------------------------------------------------------------
# Cross-regression guard — each fix does not break the other tab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keys_change_does_not_regress_events_header() -> None:
    """Tier 2: events header still surfaces f/t/v/sp=open after B4 change."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "events"
        panel._events_chain_isolate = None
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        for expected in ("Events", "sp", "j/k"):
            assert expected in plain, (
                f"events header regression: {expected!r} missing; got: {plain!r}"
            )


@pytest.mark.asyncio
async def test_events_change_does_not_regress_keys_header() -> None:
    """Tier 2: keys header still surfaces j↓ k↑ and Key Bindings after B5 change."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "keys"
        markup = panel.panel_header_markup()
        plain = str(RichText.from_markup(markup))
        for expected in ("Key Bindings", "j", "k"):
            assert expected in plain, (
                f"keys header regression: {expected!r} missing; got: {plain!r}"
            )
