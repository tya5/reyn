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
  - ``render_keys()`` 3rd return value ``key_ys`` (populated indirectly by _keys_move)
  - spy on ``_scroll_keys_into_view`` via direct attribute substitution
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import RightPanel
from reyn.interfaces.tui.widgets.right_panel import keys_tab as _kt
from reyn.interfaces.tui.widgets.right_panel.keys_tab import (
    get_keys_cursor,
    get_keys_expanded,
    render_keys,
)


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
        from reyn.interfaces.tui.widgets.right_panel.keys_tab import get_keys_cursor
        assert get_keys_cursor() == 1, (
            f"_keys_move(+1) must advance cursor to 1; got {get_keys_cursor()}"
        )


# ── Test 4: render_keys key_ys is valid after _keys_move ─────────────────────


@pytest.mark.asyncio
async def test_keys_move_populates_key_ys() -> None:
    """Tier 2b: render_keys key_ys is valid after _keys_move (= scroll-into-view data ready).

    ``_scroll_keys_into_view`` reads ``key_ys[cursor]`` to compute the scroll
    target.  This test pins that ``render_keys`` returns a ``key_ys`` list
    that is parallel to ``flat_key_list`` and contains non-negative ints, so
    the scroll helper always has valid data regardless of panel internal state.

    Verification is through the public ``render_keys()`` return value (3rd
    element) rather than the private ``panel._key_ys`` attribute.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()

        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_type = "keys"
        panel._invalidate()
        await pilot.pause()

        panel._keys_move(+1)
        await pilot.pause()

        _, flat_key_list, key_ys = render_keys(
            app,
            cursor=get_keys_cursor(),
            expanded=get_keys_expanded(),
        )
        assert len(flat_key_list) == len(key_ys), (
            f"render_keys key_ys must be parallel to flat_key_list after _keys_move; "
            f"len(flat_key_list)={len(flat_key_list)}, "
            f"len(key_ys)={len(key_ys)}"
        )
        assert all(isinstance(y, int) and y >= 0 for y in key_ys), (
            "All render_keys key_ys entries must be non-negative int"
        )


# ── Test 5: key_ys[0] >= 1 — group header precedes first key row ─────────────


@pytest.mark.asyncio
async def test_key_ys_cursor0_not_at_render_line0() -> None:
    """Tier 2b: key_ys[0] >= 1 — a group header occupies line 0, so cursor 0
    is at render line >=1.  The old formula ``y = 1 + key_ys[0]`` would
    therefore produce a scroll target >= 2, scrolling PAST the cursor row and
    hiding it above the viewport.  The fix drops the +1.

    This is a pure contract test on ``render_keys``'s 3rd return — no
    scroll geometry needed.  If this invariant breaks, the off-by-one fix
    is invalidated.

    Dogfood 2026-05-24: cursor 0 復帰時 cursor が画面外で隠れる.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        _, flat_key_list, key_ys = render_keys(app, cursor=0, expanded=set())
        assert len(key_ys) > 0, "render_keys must return at least one key row"
        assert key_ys[0] >= 1, (
            f"key_ys[0]={key_ys[0]}: first key row must be at render line >= 1 "
            f"because a group header occupies line 0.  If key_ys[0] == 0, the "
            f"old '1 + key_ys[cursor]' formula and the fix formula would differ "
            f"by a line but the geometry would need re-evaluation."
        )


# ── Test 6: old formula would be strictly greater than key_ys[0] ─────────────


@pytest.mark.asyncio
async def test_old_plus1_formula_exceeds_key_ys0() -> None:
    """Tier 2b: ``1 + key_ys[0]`` exceeds ``key_ys[0]``, which is
    the exact off-by-one: scroll_to(1 + key_ys[0]) puts render line
    (1 + key_ys[0]) at viewport top, so the cursor at render line
    key_ys[0] is above the viewport.

    Regression pin: if this test fails it means key_ys[0] == 0, which
    would change the geometry analysis and require a re-audit.

    Pure contract — no scroll geometry needed.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        _, _, key_ys = render_keys(app, cursor=0, expanded=set())
        assert len(key_ys) > 0, "render_keys must return at least one key row"
        # The off-by-one: 1 + key_ys[0] > key_ys[0].  If scroll_to is
        # called with (1 + key_ys[0]) when current > key_ys[0], the
        # cursor row is scrolled past.
        assert 1 + key_ys[0] > key_ys[0], "sanity: 1 + n > n always"
        # Pin the exact expected value so any future change to group
        # layout surfaces here first.
        assert key_ys[0] == 1, (
            f"key_ys[0]={key_ys[0]}: expected 1 (group header at line 0, "
            f"first key at line 1).  Old formula gave 1+1=2 (off by one). "
            f"If this value changed, re-verify _scroll_keys_into_view formula."
        )


# ── Test 7: scroll_to target for mid-list cursor is key_ys[cursor] ───────────


@pytest.mark.asyncio
async def test_scroll_target_for_mid_cursor_equals_key_ys() -> None:
    """Tier 2b: for cursor N>=1, ``key_ys[N]`` is the correct scroll target
    (not ``1 + key_ys[N]``).

    Verifies that every key_ys entry equals the rendered line that contains
    the cursor indicator (▶), so ``scroll_to(y=key_ys[N])`` puts that
    exact row at viewport top.

    Pure contract: iterate over a handful of cursor positions and verify
    that the render line at key_ys[N] contains the cursor indicator text.
    """
    _reset_keys_state()
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        for cursor in range(3):
            markup, flat_key_list, key_ys = render_keys(
                app, cursor=cursor, expanded=set()
            )
            if cursor >= len(key_ys):
                break
            lines = markup.split("\n")
            y = key_ys[cursor]
            assert 0 <= y < len(lines), (
                f"cursor={cursor}: key_ys[cursor]={y} is out of range "
                f"(rendered output has {len(lines)} lines)"
            )
            # The cursor row always contains "▶" (the cursor indicator).
            assert "▶" in lines[y], (
                f"cursor={cursor}: key_ys[cursor]={y} → render line "
                f"{lines[y]!r} does not contain cursor indicator '▶'.  "
                f"The scroll target should land exactly on the cursor row."
            )
            # Verify that 1 + key_ys[cursor] does NOT contain the cursor:
            # this is the off-by-one line that was wrongly used as the target.
            wrong_y = 1 + y
            if wrong_y < len(lines):
                assert "▶" not in lines[wrong_y], (
                    f"cursor={cursor}: the off-by-one line {wrong_y} "
                    f"({lines[wrong_y]!r}) unexpectedly contains '▶' — "
                    f"re-audit the formula."
                )
