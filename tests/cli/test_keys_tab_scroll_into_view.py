"""Tier 2b: Keys tab cursor scroll-into-view (dogfood regression fix).

User dogfood report 2026-05-24: Keys tab cursor moves past visible viewport
but scroll_y stays at 0.  Confirmed via Pilot (cursor 0→30, scroll_y = 0.0,
virtual_size height = 75).

Root cause: ``_keys_move`` was missing a ``_scroll_keys_into_view()`` call
(= other tabs ``_pending_move`` / ``_events_move`` have it).

Fix: ``render_keys`` now returns ``(markup, flat_key_list, key_ys)``; a
``_scroll_keys_into_view`` helper was added (same shape as other tabs); it is
called from ``_keys_move`` and included in the tab-activation dispatch table.

Public-surface-only assertions:
  - ``render_keys()`` return-tuple length / type (signature contract)
  - ``panel._key_ys`` list (populated by render_keys via _keys_move)
  - spy on ``_scroll_keys_into_view`` via direct attribute substitution
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import RightPanel
from reyn.chat.tui.widgets.right_panel import keys_tab as _kt
from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys


def _reset_keys_state() -> None:
    """Reset module-level cursor and expanded state between tests."""
    _kt._keys_cursor = 0
    _kt._keys_expanded = set()


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )


# ── Test 1: render_keys key_ys is parallel to flat_key_list ─────────────────


@pytest.mark.asyncio
async def test_render_keys_key_ys_parallel_to_flat_key_list() -> None:
    """Tier 2b: render_keys key_ys is parallel to flat_key_list (same length, non-negative ints).

    ``_scroll_keys_into_view`` reads ``_key_ys[cursor]`` where cursor indexes
    into ``flat_key_list``.  If the two lists have different lengths the lookup
    will either silently miss or raise IndexError.  Pin the parallel contract.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        markup, flat_key_list, key_ys = render_keys(app, cursor=0, expanded=set())
        assert isinstance(markup, str), "render_keys must return str markup as first element"
        assert isinstance(flat_key_list, list), "flat_key_list must be a list"
        assert isinstance(key_ys, list), "key_ys must be a list"
        assert len(flat_key_list) == len(key_ys), (
            f"flat_key_list and key_ys must be parallel lists; "
            f"len(flat_key_list)={len(flat_key_list)}, len(key_ys)={len(key_ys)}"
        )
        assert all(isinstance(y, int) and y >= 0 for y in key_ys), (
            "key_ys must contain non-negative int line numbers"
        )


# ── Test 2: key_ys values are strictly increasing ────────────────────────────


@pytest.mark.asyncio
async def test_render_keys_key_ys_strictly_increasing() -> None:
    """Tier 2b: key_ys from render_keys are strictly increasing (each key on a later line).

    Group headers and blank separator lines appear between key rows, so each
    key row must land on a strictly later output line than the previous one.
    If this fails the scroll target would jump backwards or land on the wrong row.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        _, _, key_ys = render_keys(app, cursor=0, expanded=set())
        for i in range(1, len(key_ys)):
            assert key_ys[i] > key_ys[i - 1], (
                f"key_ys must be strictly increasing; "
                f"key_ys[{i}]={key_ys[i]} <= key_ys[{i-1}]={key_ys[i-1]}"
            )


# ── Test 3: _keys_move calls _scroll_keys_into_view ─────────────────────────


@pytest.mark.asyncio
async def test_keys_move_calls_scroll_into_view() -> None:
    """Tier 2b: _keys_move(+1) invokes _scroll_keys_into_view.

    Uses a spy (direct attribute substitution per testing.ja.md — no
    unittest.mock) to verify the call contract without relying on
    headless-mode scroll geometry (``vs.size.height`` is 0 in test mode).
    Same pattern as ``test_cycle_event_tail_calls_scroll_into_view`` in
    test_events_tail_reanchor.py.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()

        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_type = "keys"
        panel._invalidate()
        await pilot.pause()

        call_count = {"n": 0}
        original = panel._scroll_keys_into_view

        def _spy() -> None:
            call_count["n"] += 1
            original()

        panel._scroll_keys_into_view = _spy  # type: ignore[method-assign]

        panel._keys_move(+1)
        await pilot.pause()
        assert call_count["n"] >= 1, (
            f"_keys_move(+1) must call _scroll_keys_into_view; "
            f"got call_count={call_count['n']}"
        )

        # Verify cursor also advanced.
        from reyn.chat.tui.widgets.right_panel.keys_tab import get_keys_cursor
        assert get_keys_cursor() == 1, (
            f"_keys_move(+1) must advance cursor to 1; got {get_keys_cursor()}"
        )


# ── Test 4: _key_ys is populated by _keys_move ───────────────────────────────


@pytest.mark.asyncio
async def test_keys_move_populates_key_ys() -> None:
    """Tier 2b: _keys_move populates _key_ys on the panel (= scroll-into-view data).

    ``_scroll_keys_into_view`` reads ``panel._key_ys[cursor]`` to compute
    the scroll target.  This test pins that ``_key_ys`` is populated with
    the same length as ``flat_key_list`` after a ``_keys_move`` call, so the
    scroll helper has valid data to work with.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()

        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_type = "keys"
        panel._invalidate()
        await pilot.pause()

        # Verify _key_ys is empty before the first move (cold state).
        assert panel._key_ys == [], (
            f"_key_ys should start empty; got {panel._key_ys[:5]}"
        )

        panel._keys_move(+1)
        await pilot.pause()

        _, flat_key_list, _ = render_keys(app)
        assert len(panel._key_ys) == len(flat_key_list), (
            f"_key_ys must be parallel to flat_key_list after _keys_move; "
            f"len(_key_ys)={len(panel._key_ys)}, "
            f"len(flat_key_list)={len(flat_key_list)}"
        )
        assert all(isinstance(y, int) and y >= 0 for y in panel._key_ys), (
            "All _key_ys entries must be non-negative int"
        )
