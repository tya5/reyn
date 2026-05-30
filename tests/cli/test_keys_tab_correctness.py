"""Tier 2: keys_tab correctness fixes — esc detail key, F3 bulk-toggle text, events i/v grouping.

Three correctness fixes (fix/tui-keys-tab-correctness):

B1 — ``_KEY_DETAILS`` was keyed ``"esc"`` but the InputBar Binding uses
     ``"escape"``, so the Esc detail block was permanently unreachable.
     Fix: key renamed ``"escape"``.

B2 — ``_KEY_DETAILS["f3"]`` described cursor-based drill-down semantics but
     the actual action (``action_skill_expand_toggle``) bulk-toggles ALL
     in-flight skill + tool-call rows.  Fix: description updated to reflect
     the real behavior.

B3 — Keys ``i`` (isolate chain) and ``v`` (toggle verbose) are events-tab
     keys (routed via ``_EVENTS_KEYS``).  They were listed in ``_PANEL_EXPLICIT``
     and therefore rendered under ``[PANEL]``, hiding their events-tab gating.
     Fix: removed from ``_PANEL_EXPLICIT``; surfaced as synthetic rows under
     ``[EVENTS (gated)]``.

All assertions use the public surface — ``render_keys()`` plain text and
``_KEY_DETAILS`` dict membership — NOT private module state.
State setup that has no dedicated public reset API (module-level cursor /
expanded set) uses the module globals directly, as per the pattern established
in ``test_keys_tab_details_and_mouse.py``.
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
    get_keys_cursor,
    get_keys_expanded,
    keys_move,
    render_keys,
    toggle_expand_cursor,
)

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


# ── B1: Esc detail key fix ────────────────────────────────────────────────────


def test_b1_escape_key_in_key_details() -> None:
    """Tier 2: _KEY_DETAILS is keyed 'escape' (not 'esc') so the lookup succeeds."""
    assert "escape" in _KEY_DETAILS, (
        "'escape' must be a key in _KEY_DETAILS (was 'esc' before B1 fix)"
    )
    assert "esc" not in _KEY_DETAILS, (
        "'esc' must NOT be a key in _KEY_DETAILS — the InputBar binding is 'escape'"
    )


@pytest.mark.asyncio
async def test_b1_esc_row_expand_detail_non_empty() -> None:
    """Tier 2: the Esc row's Space-expand detail renders non-empty after toggle.

    Before B1 the key was 'esc', but ``toggle_expand_cursor`` stores the raw
    key from ``flat_key_list`` (= 'escape' per InputBar.BINDINGS), so
    ``_KEY_DETAILS.get('escape', '')`` always returned '' — the detail block
    was permanently unreachable.
    """
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        markup, flat_key_list, _ = render_keys(app, cursor=0, expanded=set())

        assert "escape" in flat_key_list, (
            f"'escape' must appear in flat_key_list; got: {flat_key_list}"
        )
        esc_idx = flat_key_list.index("escape")
        keys_move(esc_idx, len(flat_key_list))

        # Toggle expand — must succeed (return True) and render non-empty detail.
        did_open = toggle_expand_cursor(flat_key_list)
        assert did_open, (
            "toggle_expand_cursor must return True when opening the Esc detail "
            "(returned False = detail key lookup failed, B1 fix not applied)"
        )

        markup_after, _, _ = render_keys(
            app,
            cursor=get_keys_cursor(),
            expanded=get_keys_expanded(),
        )
        # The detail text mentions context-aware back/cancel semantics.
        assert "context-aware" in markup_after.lower(), (
            f"Esc detail block must render 'context-aware' text; got:\n{markup_after}"
        )


# ── B2: F3 bulk-toggle wording ────────────────────────────────────────────────


def test_b2_f3_detail_contains_bulk_toggle_wording() -> None:
    """Tier 2: _KEY_DETAILS['f3'] reflects bulk-toggle semantics, not cursor-drill-down."""
    detail = _KEY_DETAILS.get("f3", "")
    assert detail, "_KEY_DETAILS must have an 'f3' entry"
    lower = detail.lower()
    # New wording must mention bulk behavior.
    assert "bulk" in lower or "all" in lower, (
        f"F3 detail must describe bulk-toggle behaviour; got:\n{detail}"
    )
    # Old stale wording must be absent (cursor-based drill-down).
    assert "cursor's skill row" not in lower, (
        f"F3 detail must NOT contain stale cursor-based wording; got:\n{detail}"
    )


@pytest.mark.asyncio
async def test_b2_f3_expand_renders_bulk_toggle_text() -> None:
    """Tier 2: F3 row Space-expand renders the updated bulk-toggle detail text."""
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()

        markup, flat_key_list, _ = render_keys(app, cursor=0, expanded=set())
        assert "f3" in flat_key_list, (
            f"f3 must appear in flat_key_list; got: {flat_key_list}"
        )
        f3_idx = flat_key_list.index("f3")
        keys_move(f3_idx, len(flat_key_list))

        did_open = toggle_expand_cursor(flat_key_list)
        assert did_open, "toggle_expand_cursor must return True for f3"

        markup_after, _, _ = render_keys(
            app,
            cursor=get_keys_cursor(),
            expanded=get_keys_expanded(),
        )
        # B2: detail now describes bulk-toggle, not cursor-based drill-down.
        assert "bulk-toggle" in markup_after.lower(), (
            f"F3 detail must render 'bulk-toggle' wording; got:\n{markup_after}"
        )
        # Old stale wording must not appear in the expanded detail.
        assert "cursor's skill row" not in markup_after.lower(), (
            f"F3 detail must NOT render stale cursor-based wording; got:\n{markup_after}"
        )


# ── B3: i and v under EVENTS group, not PANEL ────────────────────────────────


@pytest.mark.asyncio
async def test_b3_i_and_v_appear_under_events_group() -> None:
    """Tier 2: 'i' and 'v' render under [EVENTS (gated)], not under [PANEL].

    Before B3, both keys were in _PANEL_EXPLICIT and therefore rendered under
    the [PANEL] header, hiding the fact that they are events-tab-gated.
    After B3, they are emitted as synthetic rows under [EVENTS (gated)].
    """
    _reset_keys_state()
    app = _app()
    async with app.run_test(headless=True, size=(120, 50)) as pilot:
        await pilot.pause()

        rendered, _, _ = render_keys(app, cursor=0, expanded=set())

        events_idx = rendered.find("[EVENTS (gated)]")
        panel_idx = rendered.find("[PANEL]")
        # Both group headers must be present.
        assert events_idx >= 0, "[EVENTS (gated)] group header missing from rendered output"
        assert panel_idx >= 0, "[PANEL] group header missing from rendered output"

        # Locate the EVENTS section boundary: text between [EVENTS (gated)]
        # and the next group header ([DOCS (gated)]).
        docs_idx = rendered.find("[DOCS (gated)]")
        if docs_idx < 0:
            docs_idx = len(rendered)
        events_section = rendered[events_idx:docs_idx]

        # ``i`` and ``v`` (their descriptions) must appear inside the EVENTS section.
        assert "Isolate cursor" in events_section, (
            f"'i' (Isolate cursor) must appear in [EVENTS (gated)] section; "
            f"events_section:\n{events_section}"
        )
        assert "Toggle verbose" in events_section, (
            f"'v' (Toggle verbose) must appear in [EVENTS (gated)] section; "
            f"events_section:\n{events_section}"
        )

        # Neither description must appear in the PANEL section.
        # PANEL section: text between [PANEL] header and [EVENTS (gated)] header.
        # (_GROUP_ORDER is: GLOBAL, INPUT, CONVERSATION, PANEL, EVENTS (gated), ...)
        panel_section = rendered[panel_idx:events_idx]
        assert "Isolate cursor" not in panel_section, (
            f"'i' (Isolate cursor) must NOT appear under [PANEL]; "
            f"panel_section:\n{panel_section}"
        )
        assert "Toggle verbose" not in panel_section, (
            f"'v' (Toggle verbose) must NOT appear under [PANEL]; "
            f"panel_section:\n{panel_section}"
        )
