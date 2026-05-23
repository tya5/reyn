"""Tier 2: events tab ``i`` key isolates cursor's chain_id.

Wave-11 finding A#2. With tail=200 and multi-chain interleaving,
the events tab is unreadable when the user wants to follow one
chain's lifecycle. The blank-line chain separator helps but only
within consecutive runs of the same chain — interleaved chains
need a filter.

This adds chain isolation:
  - ``RightPanel.toggle_chain_isolate()`` captures the cursor's
    chain_id and restricts the visible list. Re-press clears.
  - ``render_events`` accepts ``chain_isolate=<chain_id>`` kwarg
    that filters ``visible`` by chain_id after the type-filter.
  - Header row "⛓ chain isolated: <prefix>" + "[i] to clear"
    surfaces the active state above the event list.
  - ``i`` key on events tab triggers the toggle via the
    ``RightPanel.on_key`` dispatch.

Public surfaces tested:
  - toggle_chain_isolate when off captures cursor's chain
  - toggle_chain_isolate when on clears
  - toggle_chain_isolate on empty list returns False
  - toggle_chain_isolate when cursor event has no chain_id
    returns False without setting isolate
  - render_events with chain_isolate filters visible list
  - Keys tab routes ``i`` correctly + lists it in the explicit
    panel keys
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_toggle_chain_isolate_captures_cursor_chain() -> None:
    """Tier 2: first ``i`` press sets isolate to cursor's chain_id."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Seed cursor + visible list (= what render_events would have
        # filled). Module-private but matches the cursor-driven contract
        # render_events writes per-tick.
        panel._events_visible = [
            {"type": "phase_started", "data": {"chain_id": "chain-A"}},
            {"type": "phase_started", "data": {"chain_id": "chain-B"}},
        ]
        panel._events_cursor = 0
        assert panel._events_chain_isolate is None
        became_active = panel.toggle_chain_isolate()
        assert became_active is True
        assert panel._events_chain_isolate == "chain-A"


@pytest.mark.asyncio
async def test_toggle_chain_isolate_second_press_clears() -> None:
    """Tier 2: re-press of ``i`` clears the active isolate."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._events_visible = [
            {"type": "phase_started", "data": {"chain_id": "chain-X"}},
        ]
        panel._events_cursor = 0
        panel.toggle_chain_isolate()
        assert panel._events_chain_isolate == "chain-X"
        # Second press clears.
        cleared = panel.toggle_chain_isolate()
        assert cleared is False
        assert panel._events_chain_isolate is None


@pytest.mark.asyncio
async def test_toggle_chain_isolate_empty_list_returns_false() -> None:
    """Tier 2: toggling with no events visible is a no-op."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._events_visible = []
        result = panel.toggle_chain_isolate()
        assert result is False
        assert panel._events_chain_isolate is None


@pytest.mark.asyncio
async def test_toggle_chain_isolate_cursor_event_no_chain_id() -> None:
    """Tier 2: cursor event with no chain_id → no-op + False return.

    Bare system events (= no chain_id in data) can't anchor an
    isolation; toggling should leave state unchanged and return
    False so the caller surfaces a hint.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._events_visible = [
            {"type": "system_event", "data": {}},  # no chain_id
        ]
        panel._events_cursor = 0
        result = panel.toggle_chain_isolate()
        assert result is False
        assert panel._events_chain_isolate is None


def test_render_events_filters_by_chain_isolate(tmp_path: Path) -> None:
    """Tier 2: ``render_events(..., chain_isolate=X)`` keeps only chain X."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    # Seed a fake events dir with 2 chains interleaved.
    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    events_root.mkdir(parents=True)
    log = events_root / "log.jsonl"
    import json
    lines = [
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:00",
         "data": {"chain_id": "A", "phase": "p1"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:01",
         "data": {"chain_id": "B", "phase": "p1"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:02",
         "data": {"chain_id": "A", "phase": "p2"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:03",
         "data": {"chain_id": "B", "phase": "p2"}},
    ]
    log.write_text("\n".join(json.dumps(item) for item in lines))

    rendered, visible, _ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,  # tail=200
        cursor=0,
    )
    # All 4 events visible without isolation.
    assert len(visible) == 4

    # With chain_isolate="A" → only chain A's 2 events visible.
    rendered_iso, visible_iso, _ys2 = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,
        cursor=0, chain_isolate="A",
    )
    assert len(visible_iso) == 2
    assert all(
        (ev.get("data") or {}).get("chain_id") == "A" for ev in visible_iso
    )
    # Rendered output surfaces the isolation banner.
    assert "chain isolated" in rendered_iso
    assert "[i]" in rendered_iso


@pytest.mark.asyncio
async def test_i_key_on_events_tab_invokes_toggle() -> None:
    """Tier 2: pressing ``i`` while events tab focused triggers the toggle."""
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel.set_panel_type("events")
        await pilot.pause()
        # Seed cursor + visible.
        panel._events_visible = [
            {"type": "phase_started", "data": {"chain_id": "C"}},
        ]
        panel._events_cursor = 0
        # Synthesise the key press.
        key_event = textual_events.Key(key="i", character="i")
        panel.on_key(key_event)
        await pilot.pause()
        assert panel._events_chain_isolate == "C"


def test_keys_tab_lists_i_under_panel_explicit() -> None:
    """Tier 2: ``i`` appears in the Keys tab's panel-key listing."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import _EVENTS_KEYS

    assert "i" in _EVENTS_KEYS


@pytest.mark.asyncio
async def test_keys_tab_render_includes_i_isolate_description() -> None:
    """Tier 2: rendered Keys tab markup surfaces the ``i`` isolate hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _ = render_keys(app)
        assert "Isolate cursor's chain" in markup or "isolate" in markup.lower()
