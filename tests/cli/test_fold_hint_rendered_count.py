"""Tier 2: fold hint reports rendered-screen-line count, not source lines (G-F6).

Wave-10 follow-up Topic G finding F6 (P2): the fold decision at
``_write_agent_markdown_with_fold`` used
``_estimate_rendered_lines(lines)`` (= rendered-screen-line space)
to gate truncation, but the hint message used
``len(lines) - _FOLD_THRESHOLD_LINES`` (= raw source-line space).
The two metrics diverge when paragraphs wrap:

  - 50-source-line reply where each paragraph wraps to 4 screen
    lines → tail ``len() - 30`` = 20, but rendered tail = ~80
    screen lines. Hint reads "20 more lines"; ``/expand`` reveals
    a screenful.
  - 60 single-word source lines past position 30 → ``len()`` =
    60, rendered ≈ 60 (no wrap fires). Report happens to match.

The hint count now routes through ``_estimate_rendered_lines`` so
both the gate and the user-facing count share one metric.

Public surfaces tested:
  - fold hint count for a wrap-heavy reply > raw source count
  - fold hint count for a no-wrap reply ≈ raw source count
    (regression guard for the single-word case)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _extract_count_from_log(conv) -> int | None:
    """Pull the ``N`` from the most recent ``[ … N more lines · …]`` fold hint."""
    import re
    log = conv._log()
    lines = [getattr(s, "text", "") for s in getattr(log, "lines", [])]
    joined = "\n".join(lines)
    match = re.search(r"…\s+(\d+)\s+more lines", joined)
    return int(match.group(1)) if match else None


@pytest.mark.asyncio
async def test_wrap_heavy_reply_reports_rendered_count_not_source_count() -> None:
    """Tier 2: long-wrap reply's hint count reflects rendered lines.

    Build a reply whose source has only ~35 newlines (= just above
    the 30-line fold threshold) but whose paragraphs each wrap to
    ~3 screen lines. Pre-fix the hint reported ``len() - 30`` = 5;
    post-fix it reports the estimated rendered count of the tail,
    which is significantly larger (= ~15 screen lines).
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # ~120-cell paragraphs → ~3 screen lines each at 80-col body
        # (minus 7-cell indent + 2-cell margin → ~71-cell body width).
        wide_para = "x " * 60  # ~120 cells per line
        lines = [wide_para] * 35  # 35 source lines, each wraps to ~2-3 rendered
        text = "\n".join(lines)
        conv._write_agent_markdown_with_fold(text)
        await pilot.pause()

        count = _extract_count_from_log(conv)
        assert count is not None, (
            "fold hint should be emitted; got no count match in log"
        )
        # Pre-fix this would be ``len(lines) - 30 = 5``. Post-fix the
        # rendered count is the estimate over the tail (= source lines
        # 30..34 wrapped), so noticeably > 5.
        assert count > 5, (
            f"wrap-heavy tail should report rendered count > source count, "
            f"got count={count} (pre-fix would have been 5)"
        )


@pytest.mark.asyncio
async def test_no_wrap_reply_count_stays_close_to_source_count() -> None:
    """Tier 2 (regression): short-line replies still get sensible counts.

    A reply of 35 single-word lines (= each fits on one screen
    line, no wrap) should report a count near 5 (= 35 - 30) — the
    metric switch shouldn't break this common case.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        text = "\n".join([f"line{i}" for i in range(35)])
        conv._write_agent_markdown_with_fold(text)
        await pilot.pause()

        count = _extract_count_from_log(conv)
        # Whether the fold fires depends on the estimate; a 35-line
        # reply where each line is "lineN" (~6 cells) gets ~35
        # rendered lines, just above the 30-line gate. The tail of 5
        # source lines renders to ~5 screen lines.
        if count is not None:
            assert count == 5, (
                f"single-word tail should report count ≈ source count, "
                f"got count={count}"
            )
