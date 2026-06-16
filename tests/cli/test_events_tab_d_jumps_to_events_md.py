"""Tier 2: events tab [d] keybinding → Docs tab cursor on runtime/events.md.

Wave-12 T2-5a (doc audit follow-on). Three invariants:

1. Events tab rendered output contains the "press [d] for events.md reference"
   footer hint so the user can discover the shortcut from the tab itself.
2. When events tab is active and ``d`` is dispatched, Docs tab becomes active
   AND its cursor lands on runtime/events.md (asserted via the public
   ``current_doc_stem()`` accessor).
3. When events tab is NOT active (e.g. memory tab) and ``d`` is dispatched,
   the events tab doesn't activate, Docs tab doesn't open (scope guard).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Test 1: footer hint is present in rendered events output ─────────────────


def test_events_tab_footer_contains_d_hint(tmp_path: Path) -> None:
    """Tier 2: render_events output ends with the [d]-for-events.md footer row."""
    from reyn.tui.widgets.right_panel.events_tab import render_events

    # Seed a minimal events log so the renderer doesn't return the early
    # "no matching events" branch (= that path also now appends the footer,
    # but testing against the non-empty path is more representative).
    events_root = tmp_path / ".reyn" / "events" / "agents" / "test" / "events"
    events_root.mkdir(parents=True)
    log = events_root / "log.jsonl"
    log.write_text(
        json.dumps({
            "type": "phase_started",
            "timestamp": "2026-05-23T10:00:00",
            "data": {"chain_id": "x", "phase": "p1"},
        }) + "\n"
    )

    rendered, _visible, _ys = render_events(
        tmp_path,
        event_filter_idx=0,  # all
        event_tail_idx=2,    # tail=100
        cursor=0,
    )
    # After escaping `[d]` becomes `\[d]` in markup — assert on the escaped form
    # (or just the surrounding text) to avoid MissingStyle regression.
    assert "press \\[d] for events.md reference" in rendered


# ── Test 2: [d] on events tab → Docs tab opens, cursor on events ─────────────


@pytest.mark.asyncio
async def test_d_key_on_events_tab_jumps_to_docs_events_md(
    tmp_path: Path,
) -> None:
    """Tier 2: ``d`` on events tab activates Docs tab with cursor on events.md."""
    from textual import events as textual_events

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import RightPanel

    # Seed a minimal docs/ tree so build_docs_index finds something.
    # Place events.md under docs/reference/runtime/ — the canonical location.
    docs_dir = tmp_path / "docs" / "reference" / "runtime"
    docs_dir.mkdir(parents=True)
    (docs_dir / "events.md").write_text("# Events\n\ntest\n")
    # Add a second file so the cursor isn't trivially at 0 by default.
    (tmp_path / "docs" / "reference" / "runtime" / "control-ir.md").write_text(
        "# Control IR\n"
    )

    app = ReynTUIApp(
        registry=None,
        agent_name="t",
        model="m",
        budget_tracker=None,
    )
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Inject project_root so build_docs_index finds our seed docs/.
        panel._project_root = tmp_path
        # Start on events tab.
        panel.set_panel_type("events")
        await pilot.pause()
        assert panel.panel_type == "events"

        # Dispatch 'd'.
        key_event = textual_events.Key(key="d", character="d")
        panel.on_key(key_event)
        await pilot.pause()

        # Docs tab must now be active.
        assert panel.panel_type == "docs", (
            f"Expected panel_type='docs', got {panel.panel_type!r}"
        )
        # Cursor must be on events.md (stem = "events").
        stem = panel.current_doc_stem()
        assert stem == "events", (
            f"Expected docs cursor on 'events', got {stem!r}"
        )


# ── Test 3: [d] on non-events tab is a scope-guard no-op ─────────────────────


@pytest.mark.asyncio
async def test_d_key_on_memory_tab_does_not_open_docs(tmp_path: Path) -> None:
    """Tier 2: ``d`` while memory tab is active leaves panel_type unchanged."""
    from textual import events as textual_events

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import RightPanel

    app = ReynTUIApp(
        registry=None,
        agent_name="t",
        model="m",
        budget_tracker=None,
    )
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._project_root = tmp_path
        # Switch to memory tab (not events).
        panel.set_panel_type("memory")
        await pilot.pause()
        assert panel.panel_type == "memory"

        # Record docs cursor before the key press.
        cursor_before = panel.docs_cursor

        # Dispatch 'd' — should be a no-op for the docs jump.
        # (Memory tab has no `d` handler, so it falls through to the
        # generic `c`-copy / `l`/`h` resize / etc. guards unchanged.)
        key_event = textual_events.Key(key="d", character="d")
        panel.on_key(key_event)
        await pilot.pause()

        # Panel must still be on memory, not docs.
        assert panel.panel_type == "memory", (
            f"Expected 'memory', got {panel.panel_type!r}"
        )
        # Docs cursor must not have moved.
        assert panel.docs_cursor == cursor_before
