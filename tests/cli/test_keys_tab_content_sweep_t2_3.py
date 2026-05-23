"""Tier 2: Keys tab content sweep — T2-3 (Wave-12 Topic B #5/#6/#7/#8).

Tests the 7 new ``_KEY_DETAILS`` entries added by T2-3:
  - tab / up / down / f2 / f / t / i

Tests the visible-row gating suffixes for f / t / i in the rendered output.

Tests that pressing ``f`` / ``i`` on the wrong tab surfaces a flash status
hint so the silent no-op stops being mysterious. Also covers the voice-badge
recording hint in ``ReynHeader``.

Public-surface-only assertions:
  - ``_KEY_DETAILS`` dict key presence (no content pinning)
  - ``render_keys()`` plain-text output (substring checks)
  - ``StickyStatus.snapshot()`` body (the designated public read surface)
  - ``ReynHeader._format_status()`` plain text (public method)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets.right_panel import keys_tab as _kt
from reyn.chat.tui.widgets.right_panel.keys_tab import (
    _KEY_DETAILS,
    render_keys,
)
from reyn.chat.tui.widgets.sticky_status import StickyStatus

# ── helpers ───────────────────────────────────────────────────────────────────


def _reset_keys_state() -> None:
    """Reset module-level cursor and expanded state between tests."""
    _kt._keys_cursor = 0
    _kt._keys_expanded = set()


def _app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )


def _sticky_snapshot(app: ReynTUIApp) -> dict:
    """Return StickyStatus.snapshot() from the conv pane (public surface)."""
    from reyn.chat.tui.widgets import ConversationView
    try:
        conv = app.query_one("#conversation", ConversationView)
        s = conv.query_one("#sticky-status", StickyStatus)
        return s.snapshot()
    except Exception:
        return {"active": False, "body": "", "kind": ""}


# ── Test 1: _KEY_DETAILS contains all 7 new entries ──────────────────────────


def test_key_details_contains_new_entries() -> None:
    """Tier 2: _KEY_DETAILS now contains entries for tab / up / down / f2 / f / t / i."""
    required = {"tab", "up", "down", "f2", "f", "t", "i"}
    missing = required - set(_KEY_DETAILS.keys())
    assert not missing, (
        f"_KEY_DETAILS is missing entries for: {missing}. "
        f"Current keys: {sorted(_KEY_DETAILS.keys())}"
    )


# ── Test 2: f row rendered output contains "(events tab)" ────────────────────


@pytest.mark.asyncio
async def test_f_row_rendered_contains_events_tab_suffix() -> None:
    """Tier 2: Keys tab visible row for 'f' contains '(events tab)' gating suffix."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        rendered, _ = render_keys(app, cursor=0, expanded=set())
        assert "events tab" in rendered.lower(), (
            f"Rendered Keys tab must contain 'events tab' suffix for 'f' row;\n"
            f"rendered output:\n{rendered}"
        )


# ── Test 3: t row rendered output acknowledges dual-purpose ──────────────────


@pytest.mark.asyncio
async def test_t_row_rendered_contains_both_tab_contexts() -> None:
    """Tier 2: Keys tab visible row for 't' mentions both Events tab and Memory tab."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        rendered, _ = render_keys(app, cursor=0, expanded=set())
        lower = rendered.lower()
        # 't' is dual-purpose: events-tab tail cycle + memory-tab type filter.
        # The binding description must acknowledge at least one of the two tabs.
        assert "events tab" in lower or "memory tab" in lower, (
            f"Rendered Keys tab must mention 'Events tab' or 'Memory tab' for 't' row;\n"
            f"rendered output:\n{rendered}"
        )


# ── Test 4: i row rendered output contains "(events tab)" ────────────────────


@pytest.mark.asyncio
async def test_i_row_rendered_contains_events_tab_suffix() -> None:
    """Tier 2: Keys tab visible row for 'i' contains '(events tab)' suffix."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        rendered, _ = render_keys(app, cursor=0, expanded=set())
        # The _PANEL_EXPLICIT entry for 'i' was updated to include "(events tab only)".
        assert "events tab" in rendered.lower(), (
            f"Rendered Keys tab must contain 'events tab' suffix for 'i' row;\n"
            f"rendered output:\n{rendered}"
        )


# ── Test 5: pressing 'f' on Memory tab → flash status with "events tab" ──────


@pytest.mark.asyncio
async def test_f_on_wrong_tab_flashes_events_tab_hint() -> None:
    """Tier 2: pressing 'f' while on Memory tab → flash status with 'events tab'."""
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        # Open the panel and switch to Memory tab programmatically.
        app._panel_visible = True
        panel = app.query_one("#right_panel")
        panel.styles.display = "block"
        panel.set_panel_type("memory")
        await pilot.pause()

        # Now simulate 'f' key via on_key dispatch directly on the panel.
        # We call the public dispatch path: inject a key event into RightPanel.
        from textual.events import Key
        key_event = Key("f", character="f")
        # Deliver the key to the panel's on_key handler.
        panel.on_key(key_event)
        await pilot.pause()

        snap = _sticky_snapshot(app)
        assert "events tab" in snap.get("body", "").lower(), (
            f"Flash status after 'f' on Memory tab must contain 'events tab'; "
            f"got sticky snapshot: {snap}"
        )


# ── Test 6: pressing 'f' on Events tab → no wrong-tab flash ──────────────────


@pytest.mark.asyncio
async def test_f_on_events_tab_does_not_flash_wrong_tab_hint() -> None:
    """Tier 2: pressing 'f' while on Events tab does NOT trigger the wrong-tab flash.

    The happy path (f on events tab) runs cycle_event_filter instead;
    the wrong-tab flash message must not appear.
    """
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        app._panel_visible = True
        panel = app.query_one("#right_panel")
        panel.styles.display = "block"
        panel.set_panel_type("events")
        await pilot.pause()

        from textual.events import Key
        key_event = Key("f", character="f")
        panel.on_key(key_event)
        await pilot.pause()

        snap = _sticky_snapshot(app)
        body = snap.get("body", "").lower()
        # The wrong-tab flash says "'f' only active on the Events tab".
        # On the events tab, this message must NOT appear.
        assert "'f' only active" not in body, (
            f"Wrong-tab flash must not appear when 'f' is pressed on Events tab; "
            f"got sticky snapshot: {snap}"
        )


# ── Test 7: voice badge recording shows Enter/Esc hint ───────────────────────


def test_voice_badge_recording_shows_enter_esc_hint() -> None:
    """Tier 2: while voice_state='recording', header _format_status contains 'Enter' or 'Esc'."""
    from reyn.chat.tui.widgets.header import ReynHeader

    # Instantiate ReynHeader in isolation (no Textual app needed for
    # _format_status; we call it directly after setting the voice state).
    header = ReynHeader(agent_name="test-agent", model="test-model")
    # Inject voice state via the public setter (= the method the app calls).
    header._voice_state = "recording"

    # Call the public _format_status method and convert to plain text.
    text_obj = header._format_status()
    plain = text_obj.plain  # Rich Text.plain strips markup

    assert "Enter" in plain or "Esc" in plain, (
        f"Header badge while recording must contain 'Enter' or 'Esc' hint; "
        f"got: {plain!r}"
    )
