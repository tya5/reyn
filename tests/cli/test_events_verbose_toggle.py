"""Tier 2: events tab verbose toggle — hide compaction_check by default + v key.

compaction_check events fire on every chat turn but the vast majority carry
"didn't compact" outcomes (too_few_turns / below_min_batch / below_threshold /
already_running). The actual lifecycle is covered by compaction_started /
compaction_completed / compaction_failed. Default-hide the noise; bind v on
the events tab to toggle verbose mode.

Public surfaces tested:
  1. render_events(verbose=False) with mixed events → compaction_check absent,
     others present.
  2. render_events(verbose=True) → compaction_check present.
  3. render_events(3 compaction_check + 1 other, verbose=False) → footer
     "3 compaction_check hidden".
  4. render_events(0 compaction_check, verbose=False) → no footer.
  5. on_key ``v`` on events tab → _events_verbose toggles + render reflects.
  6. on_key ``v`` on memory tab → _events_verbose unchanged (scope guard).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_events(events_root: Path, events: list[dict]) -> None:
    """Write events as a JSONL file under events_root."""
    events_root.mkdir(parents=True, exist_ok=True)
    log = events_root / "log.jsonl"
    log.write_text("\n".join(json.dumps(ev) for ev in events), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — verbose=False hides compaction_check, shows other events
# ---------------------------------------------------------------------------

def test_render_events_verbose_false_hides_compaction_check(tmp_path: Path) -> None:
    """Tier 2: verbose=False suppresses compaction_check, keeps other types."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    _write_events(events_root, [
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:00",
         "data": {"outcome": "too_few_turns"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:01",
         "data": {"phase": "analyse"}},
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:02",
         "data": {"outcome": "below_threshold"}},
        {"type": "llm_called", "timestamp": "2026-05-23T10:00:03",
         "data": {"phase": "analyse"}},
    ])

    rendered, visible, _ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,
        cursor=0, verbose=False,
    )

    # compaction_check must not appear in visible list
    types = [ev.get("type") for ev in visible]
    assert "compaction_check" not in types, (
        f"compaction_check should be hidden when verbose=False; got types={types}"
    )

    # Other events must be present
    assert "phase_started" in types
    assert "llm_called" in types

    # Rendered markup must not contain compaction_check event rows
    # (the type string appears in the color dict + footer; check the
    # rendered output for the event-row pattern which pairs timestamp + type)
    assert "compaction_check" not in rendered or "hidden" in rendered, (
        "compaction_check should not appear as a visible event row when verbose=False"
    )


# ---------------------------------------------------------------------------
# Test 2 — verbose=True includes compaction_check
# ---------------------------------------------------------------------------

def test_render_events_verbose_true_shows_compaction_check(tmp_path: Path) -> None:
    """Tier 2: verbose=True includes compaction_check events in visible list."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    _write_events(events_root, [
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:00",
         "data": {"outcome": "too_few_turns"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:01",
         "data": {"phase": "analyse"}},
    ])

    rendered, visible, _ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,
        cursor=0, verbose=True,
    )

    types = [ev.get("type") for ev in visible]
    assert "compaction_check" in types, (
        f"compaction_check should appear when verbose=True; got types={types}"
    )
    assert "phase_started" in types

    # Rendered markup should show compaction_check event row
    assert "compaction_check" in rendered


# ---------------------------------------------------------------------------
# Test 3 — footer shows hidden count when N > 0
# ---------------------------------------------------------------------------

def test_render_events_verbose_false_footer_shows_hidden_count(tmp_path: Path) -> None:
    """Tier 2: footer surfaces count of suppressed compaction_check events."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    _write_events(events_root, [
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:00",
         "data": {"outcome": "too_few_turns"}},
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:01",
         "data": {"outcome": "below_min_batch"}},
        {"type": "compaction_check", "timestamp": "2026-05-23T10:00:02",
         "data": {"outcome": "already_running"}},
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:03",
         "data": {"phase": "analyse"}},
    ])

    rendered, visible, _ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,
        cursor=0, verbose=False,
    )

    # Footer must mention 3 hidden + the [v] toggle cue
    assert "3 compaction_check hidden" in rendered, (
        f"Expected '3 compaction_check hidden' in footer; rendered=\n{rendered}"
    )
    assert "[v]" in rendered, (
        f"Expected '[v] to show' cue in footer; rendered=\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Test 4 — no footer when 0 compaction_check events
# ---------------------------------------------------------------------------

def test_render_events_verbose_false_no_footer_when_zero_hidden(tmp_path: Path) -> None:
    """Tier 2: footer not added when no compaction_check events were suppressed."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    _write_events(events_root, [
        {"type": "phase_started", "timestamp": "2026-05-23T10:00:00",
         "data": {"phase": "analyse"}},
        {"type": "llm_called", "timestamp": "2026-05-23T10:00:01",
         "data": {"phase": "analyse"}},
    ])

    rendered, visible, _ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2,
        cursor=0, verbose=False,
    )

    assert "compaction_check hidden" not in rendered, (
        "No compaction_check hidden footer should appear when none were suppressed"
    )
    assert "[v] to show" not in rendered


# ---------------------------------------------------------------------------
# Test 5 — v key on events tab toggles _events_verbose
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v_key_on_events_tab_toggles_verbose() -> None:
    """Tier 2: pressing v on the events tab flips _events_verbose."""
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel.set_panel_type("events")
        await pilot.pause()

        # Default state is False (= hide compaction_check)
        assert panel._events_verbose is False

        # First v press → True
        key_event = textual_events.Key(key="v", character="v")
        panel.on_key(key_event)
        await pilot.pause()
        assert panel._events_verbose is True, (
            "Expected _events_verbose=True after first v press on events tab"
        )

        # Second v press → False again
        key_event2 = textual_events.Key(key="v", character="v")
        panel.on_key(key_event2)
        await pilot.pause()
        assert panel._events_verbose is False, (
            "Expected _events_verbose=False after second v press on events tab"
        )


# ---------------------------------------------------------------------------
# Test 6 — v key on memory tab leaves _events_verbose unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v_key_on_memory_tab_does_not_toggle_verbose() -> None:
    """Tier 2: pressing v on the memory tab must not change _events_verbose."""
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel.set_panel_type("memory")
        await pilot.pause()

        assert panel._events_verbose is False

        key_event = textual_events.Key(key="v", character="v")
        panel.on_key(key_event)
        await pilot.pause()

        # Must still be False — the v handler is events-tab-only
        assert panel._events_verbose is False, (
            "_events_verbose must not change when v is pressed on a non-events tab"
        )


# ---------------------------------------------------------------------------
# Keys tab registrations
# ---------------------------------------------------------------------------

def test_v_in_events_keys_set() -> None:
    """Tier 2: v is registered in _EVENTS_KEYS (= EVENTS (gated) group)."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import _EVENTS_KEYS

    assert "v" in _EVENTS_KEYS


def test_v_in_key_details() -> None:
    """Tier 2: v has a detail entry in _KEY_DETAILS."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import _KEY_DETAILS

    assert "v" in _KEY_DETAILS
    assert "verbose" in _KEY_DETAILS["v"].lower()


# ---------------------------------------------------------------------------
# Test 7 — footer [d] hint renders without MissingStyle (Tier 2b escape check)
# ---------------------------------------------------------------------------

def test_render_events_footer_d_hint_no_missing_style(tmp_path: Path) -> None:
    """Tier 2b: render_events footer [d] hint is escaped (no MissingStyle).

    Rich raises MissingStyle when [d] is interpreted as a style tag.
    Escape must be present so Text.from_markup parses cleanly.
    """
    from rich.text import Text

    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    events_root.mkdir(parents=True, exist_ok=True)
    (events_root / "log.jsonl").write_text(
        '{"type": "phase_started", "timestamp": "2026-05-24T10:00:00", "data": {}}',
        encoding="utf-8",
    )

    rendered, _, _ = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2, cursor=0, verbose=False,
    )

    # Must not raise MissingStyle — if [d] is unescaped this blows up
    Text.from_markup(rendered)

    # Sanity: the literal text "[d]" still appears in the rendered string
    # (after unescaping by Rich) — but the markup itself must be valid.
    assert "events.md reference" in rendered


# ---------------------------------------------------------------------------
# Test 8 — footer [v] hint renders without MissingStyle (Tier 2b escape check)
# ---------------------------------------------------------------------------

def test_render_events_footer_v_hint_no_missing_style(tmp_path: Path) -> None:
    """Tier 2b: render_events footer [v] hint is escaped (no MissingStyle).

    When n_compaction_check_hidden > 0 and verbose=False the footer appends
    '([v] to show)'. Rich raises MissingStyle if [v] is unescaped.
    """
    from rich.text import Text

    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    events_root.mkdir(parents=True, exist_ok=True)
    # Mix: one visible event + three compaction_check → hidden count = 3
    (events_root / "log.jsonl").write_text(
        "\n".join([
            '{"type": "compaction_check", "timestamp": "2026-05-24T10:00:00", "data": {"outcome": "too_few_turns"}}',
            '{"type": "compaction_check", "timestamp": "2026-05-24T10:00:01", "data": {"outcome": "below_threshold"}}',
            '{"type": "compaction_check", "timestamp": "2026-05-24T10:00:02", "data": {"outcome": "already_running"}}',
            '{"type": "phase_started", "timestamp": "2026-05-24T10:00:03", "data": {}}',
        ]),
        encoding="utf-8",
    )

    rendered, _, _ = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=2, cursor=0, verbose=False,
    )

    # Must not raise MissingStyle — if [v] is unescaped this blows up
    Text.from_markup(rendered)

    # Sanity: footer must mention the hidden count
    assert "compaction_check hidden" in rendered


@pytest.mark.asyncio
async def test_keys_tab_render_includes_v_description() -> None:
    """Tier 2: rendered Keys tab markup surfaces the v verbose hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _ = render_keys(app)
        assert "verbose" in markup.lower() or "Toggle verbose" in markup, (
            "Keys tab markup should surface the v=Toggle verbose entry"
        )
