"""Tier 2b: render_events returns per-row y-coords accounting for multi-line drift (A-F3).

Wave-8 Topic A finding F3 (P2): pre-fix ``_scroll_events_into_view``
used ``y = 1 + cursor`` which assumed one rendered line per event.
But the actual rendered output has two sources of drift:

  - chain-switch blank line between events that don't share chain_id
  - extra ``↳`` reply line under each ``user_message_received`` row

After a few chains, the arithmetic projection accumulated 3-5 lines
of drift, leaving the cursor below the viewport with no apparent
movement on j/k. Fix mirrors memory_tab / agents_tab: track each
event's actual y-coord during render and look it up at scroll time.
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _write_events(tmp_path: Path, events: list[dict]) -> None:
    """Write one jsonl per event under agents/default/ so the loader picks them up."""
    events_root = tmp_path / ".reyn" / "events" / "agents" / "default"
    events_root.mkdir(parents=True, exist_ok=True)
    log = events_root / "session.jsonl"
    log.write_text(
        "\n".join(_json.dumps(ev) for ev in events) + "\n",
        encoding="utf-8",
    )


def test_event_ys_returned_for_single_chain_no_blanks(tmp_path: Path) -> None:
    """Tier 2b: same chain_id → no blank lines → ys are contiguous."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    _write_events(tmp_path, [
        {"type": "phase_started", "timestamp": f"2026-05-22T10:00:0{i}Z",
         "data": {"chain_id": "c1", "phase": f"p{i}"}}
        for i in range(3)
    ])
    rendered, visible, ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=0, cursor=0,
        cache={}, filelist_cache=None,
    )
    # Single chain, no user_message_received → ys are 0, 1, 2.
    assert ys == [0, 1, 2]
    assert len(visible) == len(ys)
    # Sanity-check: ys index into actual rendered lines.
    lines = rendered.split("\n")
    for i, y in enumerate(ys):
        assert "phase_started" in lines[y], (
            f"event {i} (y={y}) should land on a phase_started row, got: {lines[y]!r}"
        )


def test_event_ys_skips_blank_line_at_chain_switch(tmp_path: Path) -> None:
    """Tier 2b: chain switch inserts blank → ys[1] = 2 (not 1).

    Without the per-row tracking, ``y = 1 + cursor`` would point at
    the blank separator row rather than the second event's headline.
    """
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    _write_events(tmp_path, [
        {"type": "phase_started", "timestamp": "2026-05-22T10:00:00Z",
         "data": {"chain_id": "c1", "phase": "p0"}},
        {"type": "phase_started", "timestamp": "2026-05-22T10:00:01Z",
         "data": {"chain_id": "c2", "phase": "p1"}},
        {"type": "phase_started", "timestamp": "2026-05-22T10:00:02Z",
         "data": {"chain_id": "c2", "phase": "p2"}},
    ])
    rendered, visible, ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=0, cursor=0,
        cache={}, filelist_cache=None,
    )
    # visible is newest-first: [p2 (c2), p1 (c2), p0 (c1)]
    # rendered: ▶ p2  /  p1  /  <blank>  /  p0
    assert ys[0] == 0
    assert ys[1] == 1
    assert ys[2] == 3, f"chain switch should bump y to 3, got {ys!r}"
    lines = rendered.split("\n")
    assert lines[2] == "", "row 2 should be the blank chain-switch separator"
    # Each event's y must land on its actual headline (= phase_started row).
    for i, y in enumerate(ys):
        assert "phase_started" in lines[y]


def test_event_ys_accounts_for_user_message_reply_line(tmp_path: Path) -> None:
    """Tier 2b: user_message_received adds a ↳ line → subsequent ys bump by 1.

    Even when there's no chain switch, a user_message_received row
    consumes 2 rendered lines (headline + ``↳`` reply), so the next
    event's y is 2 not 1.
    """
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    _write_events(tmp_path, [
        {"type": "user_message_received",
         "timestamp": "2026-05-22T10:00:00Z",
         "data": {"chain_id": "c1", "text": "hello"}},
        {"type": "phase_started",
         "timestamp": "2026-05-22T10:00:01Z",
         "data": {"chain_id": "c1", "phase": "p1"}},
    ])
    rendered, visible, ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=0, cursor=0,
        cache={}, filelist_cache=None,
    )
    # visible newest-first: [phase_started, user_message_received]
    # rendered:
    #   row 0 → phase_started
    #   row 1 → user_message_received headline
    #   row 2 → ↳ (awaiting…)
    assert ys[0] == 0
    assert ys[1] == 1
    lines = rendered.split("\n")
    assert "↳" in lines[2], (
        f"reply line should follow user_message_received, got rendered={rendered!r}"
    )


def test_empty_visible_returns_empty_ys(tmp_path: Path) -> None:
    """Tier 2b: empty / no-match return shape stays a 3-tuple."""
    from reyn.chat.tui.widgets.right_panel.events_tab import render_events

    # No events root → "no events yet" early return.
    rendered, visible, ys = render_events(
        tmp_path, event_filter_idx=0, event_tail_idx=0, cursor=0,
        cache={}, filelist_cache=None,
    )
    assert visible == []
    assert ys == []
    assert isinstance(rendered, str)
