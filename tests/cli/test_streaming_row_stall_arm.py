"""Tier 2: StreamingRow arms stall indicator from stream open (F-F2).

Wave-9 Topic F finding F2 (P1): the sticky ``⟳ thinking…``
indicator hid at ``__stream_start__`` (= before the first token).
``_last_chunk_at`` was initialised to ``0.0`` as a sentinel, and
``_build_renderable`` used ``idle = 0.0`` whenever the sentinel
was unchanged — disabling the stall ``  …`` cue until the first
token arrived. A cold-start LLM call (6-20s first-token latency
is realistic for long-context queries) left the user staring at
only a blinking cursor with no "still waiting" feedback during
the most expensive part of the call.

After the fix ``_last_chunk_at`` is set to ``monotonic()`` at
construction so the idle calculation is meaningful from open.
The standard 5s stall threshold now fires during a slow first
token too.

Public surfaces tested:
  - immediately after construction, idle < 5s → cursor (not stall)
  - back-dating ``_last_chunk_at`` by 6s → stall ``  …`` fires
  - first ``append()`` then back-dating still triggers stall (= the
    post-first-token path was already working; this is the regression
    guard that we didn't break it)
"""
from __future__ import annotations

import sys
from pathlib import Path
from time import monotonic

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_freshly_constructed_row_renders_cursor_not_stall() -> None:
    """Tier 2: a new row with no tokens shows the cursor, not the stall cue."""
    from reyn.tui.widgets.streaming_row import StreamingRow

    row = StreamingRow(prefix="")
    rendered = row.build_renderable().plain
    # Cursor glyph present; stall ellipsis absent.
    assert "▍" in rendered, (
        f"new row should render cursor, got plain={rendered!r}"
    )
    assert " …" not in rendered, (
        f"new row should NOT render stall cue (idle < 5s), got: {rendered!r}"
    )


def test_open_with_no_tokens_after_six_seconds_renders_stall_cue() -> None:
    """Tier 2: 6s of no tokens since open shows the stall ``  …`` cue.

    The fix arms ``_last_chunk_at`` from construction. Back-date it
    by 6s to simulate "first token still pending after 6s of cold
    start" without actually sleeping in the test.
    """
    from reyn.tui.widgets.streaming_row import StreamingRow

    row = StreamingRow(prefix="")
    # Simulate 6s elapsed since open with NO append() ever called.
    row._last_chunk_at = monotonic() - 6.0
    rendered = row.build_renderable().plain
    assert "…" in rendered, (
        f"6s without first token should fire stall cue, got: {rendered!r}"
    )
    assert "▍" not in rendered, (
        f"stall path should drop the cursor, got: {rendered!r}"
    )


def test_chunk_arrival_resets_idle_timer() -> None:
    """Tier 2: ``append()`` updates ``_last_chunk_at`` so the cursor returns.

    Pre-fix this branch worked; we keep it as a regression guard so
    later refactors of the idle path can't silently disable the
    post-first-token cursor reset.
    """
    from reyn.tui.widgets.streaming_row import StreamingRow

    row = StreamingRow(prefix="")
    row._last_chunk_at = monotonic() - 6.0  # stall would fire here
    assert "…" in row.build_renderable().plain

    row.append("hello")  # fresh chunk — _last_chunk_at = monotonic()
    rendered = row.build_renderable().plain
    assert "▍" in rendered, (
        f"after chunk arrival, cursor should return, got: {rendered!r}"
    )
    assert " …" not in rendered, (
        f"stall cue should clear after chunk, got: {rendered!r}"
    )
    assert "hello" in rendered
