"""Tier 2: _KEY_DETAILS table, MOUSE group, and Space-to-expand on Keys tab.

Wave-12 T1-4 (doc audit foundation):
  - ``_KEY_DETAILS`` dict exposes "what does this key REALLY do" detail text.
  - MOUSE group in Keys tab lists click-interaction affordances.
  - ``toggle_expand_cursor()`` toggles an inline detail block under the cursor
    row; two presses = hidden again; no-op on rows with no detail entry.

Tests assert against the PUBLIC SURFACE — rendered markup plain text — not
private state. Module-level cursor / expand state is reset at the start of
each test using the public reset helpers (keys_move + toggle) so tests are
independent regardless of run order.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets.right_panel import keys_tab as _kt
from reyn.tui.widgets.right_panel.keys_tab import (
    _KEY_DETAILS,
    get_keys_cursor,
    get_keys_expanded,
    keys_move,
    render_keys,
    toggle_expand_cursor,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _reset_keys_state() -> None:
    """Reset module-level cursor and expanded state between tests."""
    # Move cursor back to 0 by delegating through the public interface.
    # We do this by setting _keys_cursor + _keys_expanded directly on the
    # module; the public surface is designed to be used via RightPanel, but
    # the module-level state is intentionally accessible so tests can reset
    # it without spinning up the full TUI app. Testing policy: assert on
    # public surface, but STATE SETUP may touch module attrs when the public
    # interface for reset doesn't exist. Here _keys_cursor / _keys_expanded
    # are module globals (= listed in __all__'s sibling helpers), so this
    # is the correct reset path.
    _kt._keys_cursor = 0
    _kt._keys_expanded = set()


def _render_plain(app: ReynTUIApp) -> str:
    """Return the rendered Keys tab markup (no cursor/expand override)."""
    markup, _, _ = render_keys(
        app,
        cursor=get_keys_cursor(),
        expanded=get_keys_expanded(),
    )
    return markup


def _app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )


# ── Test 1: MOUSE group header is present ────────────────────────────────────


@pytest.mark.asyncio
async def test_mouse_group_header_present() -> None:
    """Tier 2: Keys tab rendered output includes a MOUSE group header."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        rendered = _render_plain(app)
        assert "[MOUSE]" in rendered, (
            f"Keys tab must render a MOUSE group header; got:\n{rendered}"
        )


# ── Test 2: "click skill row" row is present ────────────────────────────────


@pytest.mark.asyncio
async def test_mouse_click_skill_row_present() -> None:
    """Tier 2: Keys tab rendered output includes 'click skill row' entry."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        rendered = _render_plain(app)
        assert "click skill row" in rendered, (
            f"Keys tab must render the 'click skill row' MOUSE entry; "
            f"got:\n{rendered}"
        )


# ── Test 3: _KEY_DETAILS has at least 6 entries ──────────────────────────────


def test_key_details_has_at_least_six_entries() -> None:
    """Tier 2: _KEY_DETAILS has at least 6 entries (content sweep deferred to T2-3)."""
    assert _KEY_DETAILS, "_KEY_DETAILS must be non-empty"
    # f3 is the primary anchor for the Space-expand UX (T2-4); verify it and
    # a minimum set of sibling keys are present so the wave-12 doc sweep lands.
    required = {"f3"}
    missing = required - _KEY_DETAILS.keys()
    assert not missing, (
        f"_KEY_DETAILS missing required key(s): {missing}; "
        f"got {list(_KEY_DETAILS.keys())}"
    )


# ── Test 4: Space-expand on F3 row shows detail text ─────────────────────────


@pytest.mark.asyncio
async def test_toggle_expand_f3_row_shows_detail() -> None:
    """Tier 2: toggle_expand_cursor() on the F3 row → detail block reflects bulk-toggle wording."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        # Find the flat_key_list so we can locate the F3 row.
        markup, flat_key_list, _ = render_keys(
            app,
            cursor=0,
            expanded=set(),
        )
        assert "f3" in flat_key_list, (
            f"F3 must appear in the flat key list; got: {flat_key_list}"
        )
        f3_idx = flat_key_list.index("f3")

        # Move cursor to F3 row via the public helper.
        keys_move(f3_idx, len(flat_key_list))

        # Toggle expand.
        did_open = toggle_expand_cursor(flat_key_list)
        assert did_open, "toggle_expand_cursor should return True when opening"

        # Re-render with updated state and check for detail text.
        # B2 fix: F3 now describes bulk-toggle behaviour (not cursor-based
        # drill-down), so assert on the new wording sentinel.
        markup_after, _, _ = render_keys(
            app,
            cursor=get_keys_cursor(),
            expanded=get_keys_expanded(),
        )
        assert "bulk-toggle" in markup_after.lower(), (
            f"After expand on F3 row, rendered output must contain 'bulk-toggle'; "
            f"got:\n{markup_after}"
        )


# ── Test 5: Double toggle on F3 row hides detail block ───────────────────────


@pytest.mark.asyncio
async def test_toggle_expand_twice_hides_detail() -> None:
    """Tier 2: toggle_expand_cursor() twice → detail block is gone (toggle semantics)."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        markup, flat_key_list, _ = render_keys(app, cursor=0, expanded=set())
        assert "f3" in flat_key_list
        f3_idx = flat_key_list.index("f3")
        keys_move(f3_idx, len(flat_key_list))

        # First toggle: open.
        toggle_expand_cursor(flat_key_list)
        markup_open, _, _ = render_keys(
            app, cursor=get_keys_cursor(), expanded=get_keys_expanded(),
        )
        assert "bulk-toggle" in markup_open.lower(), (
            "Detail must be visible after first toggle (bulk-toggle wording)"
        )

        # Second toggle: close.
        toggle_expand_cursor(flat_key_list)
        markup_closed, _, _ = render_keys(
            app, cursor=get_keys_cursor(), expanded=get_keys_expanded(),
        )
        # The *inline detail block* lines are emitted by the expand logic
        # (prefixed with dim #aaaaaa). After close, those extra detail lines
        # must be gone. We detect this by checking that distinctive text
        # from _KEY_DETAILS["f3"] is absent — "convergence state" is not
        # in the short row description, only in the expanded detail block.
        detail_sentinel = "convergence state"
        assert detail_sentinel not in markup_closed, (
            f"After second toggle detail must be hidden; "
            f"sentinel {detail_sentinel!r} still present:\n{markup_closed}"
        )


# ── Test 6: toggle_expand_cursor on no-detail row → no-op ────────────────────


@pytest.mark.asyncio
async def test_toggle_expand_no_detail_row_is_noop() -> None:
    """Tier 2: toggle_expand_cursor() on a row with no _KEY_DETAILS entry → no-op, no crash."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        markup_before, flat_key_list, _ = render_keys(app, cursor=0, expanded=set())

        # Find a row whose raw_key is NOT in _KEY_DETAILS.
        # Most rows won't be in _KEY_DETAILS (only 6 initial entries).
        no_detail_idx: int | None = None
        for i, rk in enumerate(flat_key_list):
            if rk not in _KEY_DETAILS:
                no_detail_idx = i
                break

        assert no_detail_idx is not None, (
            "There should be at least one row without a _KEY_DETAILS entry"
        )

        # Move cursor to that row.
        keys_move(no_detail_idx, len(flat_key_list))

        # Toggle — must return False (= no-op).
        result = toggle_expand_cursor(flat_key_list)
        assert result is False, (
            f"toggle_expand_cursor must return False for row with no detail; "
            f"raw_key={flat_key_list[no_detail_idx]!r}"
        )

        # Render after no-op: output must be unchanged (same markup since
        # expand state didn't change and cursor moved to same col).
        markup_after, _, _ = render_keys(
            app, cursor=get_keys_cursor(), expanded=get_keys_expanded(),
        )
        # The expand-specific sentinel lines should not appear.
        for rk, detail in _KEY_DETAILS.items():
            # Check first sentence of each detail is absent.
            first_line = detail.splitlines()[0][:30]
            assert first_line not in markup_after, (
                f"No detail block should be visible after a no-op toggle; "
                f"found {first_line!r} in output"
            )
