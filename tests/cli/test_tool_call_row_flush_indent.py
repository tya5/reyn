"""Tier 2: flushed ToolCallRow with label_prefix renders WITHOUT double-indent.

A4 fix contract: when a ToolCallRow carries a non-empty ``label_prefix``
(= sub-skill nesting, e.g. ``"  └─ "``), the prefix is baked into the
Rich Text from ``_build_line1()``.  The flush path must NOT add an extra
hanging-indent Padding on top — that would produce double-indent vs the
live widget.

Public surfaces tested:
  - ``render_line1().plain`` (= public accessor for the built text)
  - Leading whitespace of the flushed line (= col-position signal)
  - Top-level rows (empty prefix) path is unchanged: the test asserts
    the prefix-present path differs from the prefix-absent path in
    leading whitespace, proving the two code paths diverge correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest
from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _line_text(log: RichLog, index: int) -> str:
    return "".join(seg.text for seg in log.lines[index])


def _find_line_containing(log: RichLog, needle: str) -> int:
    for i, strip in enumerate(log.lines):
        if needle in "".join(seg.text for seg in strip):
            return i
    raise AssertionError(f"{needle!r} never appeared in RichLog")


def _row_with_prefix(label_prefix: str = "  └─ "):
    """Unmounted ToolCallRow with label_prefix for direct render testing."""
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(
        tool_name="bash",
        args_repr="cmd=ls",
        label_prefix=label_prefix,
    )
    row.finish_success(result_snippet="ok")
    return row


def _row_no_prefix():
    """Unmounted ToolCallRow without label_prefix (top-level)."""
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(
        tool_name="bash",
        args_repr="cmd=ls",
        label_prefix="",
    )
    row.finish_success(result_snippet="ok")
    return row


def test_prefixed_row_line1_starts_with_label_prefix() -> None:
    """Tier 2: _build_line1() on a prefixed row opens with the label_prefix text.

    This is the anchor invariant for the A4 fix: the prefix is baked into
    the Rich Text that the flush path writes to the RichLog.  The flush
    path must use _write_log (col-0) for these rows, not _write_body
    (which would add an extra _indent_body Padding).
    """
    row = _row_with_prefix("  └─ ")
    line1_plain = row.render_line1().plain
    # The prefix appears at the very start of the rendered text.
    assert line1_plain.startswith("  └─ "), (
        f"prefixed row line1 must start with the label_prefix; got {line1_plain!r}"
    )


def test_top_level_row_line1_does_not_start_with_prefix() -> None:
    """Tier 2: _build_line1() on a top-level row has NO label_prefix leading text.

    Verifies the row's own text is col-0 origin (no self-indent). Per #1245
    the flush path now writes top-level rows via ``_write_log`` (col 0) too,
    matching the live widget — see ``test_flushed_toplevel_tool_row_lands_at_col0``.
    """
    row = _row_no_prefix()
    line1_plain = row.render_line1().plain
    # Top-level rows start directly with the state glyph (no prefix spaces).
    assert not line1_plain.startswith("  └─ "), (
        f"top-level row must not start with sub-skill prefix; got {line1_plain!r}"
    )
    # The line starts with the state glyph (no leading spaces from ToolCallRow).
    assert not line1_plain.startswith(" "), (
        f"top-level ToolCallRow must not self-indent; got {line1_plain!r}"
    )


@pytest.mark.asyncio
async def test_flushed_prefixed_row_lands_at_prefix_not_double_indent() -> None:
    """Tier 2b: A4 regression guard — flushing a sub-skill ToolCallRow lands at
    the bare ``label_prefix`` column, NOT an 8-col hanging-indent + prefix.

    This exercises the REAL ``_do_flush_tool_call_row`` path (the fix's
    actual code): the prefixed branch uses ``_write_log`` (no Padding).
    Reverting that branch to ``_write_body`` would wrap the line in an
    8-cell left ``Padding``, so the rendered RichLog line would start with
    8 spaces — and this test would FAIL.  (The prior ``render_line1()``-only
    check could not catch that, because the Padding lives OUTSIDE the Rich
    Text and is not visible in ``.plain``.)
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        row = _row_with_prefix("  └─ ")
        conv.mount(row)
        await pilot.pause()
        conv._do_flush_tool_call_row(row)
        await pilot.pause()

        idx = _find_line_containing(log, "bash")
        line = _line_text(log, idx)
        # Bare prefix at the start — matches the live widget's column.
        assert line.startswith("  └─ "), (
            f"flushed prefixed row must start with the bare label_prefix; got {line!r}"
        )
        # Regression signal: ``_write_body`` would prepend an 8-cell Padding.
        assert not line.startswith(" " * 8), (
            f"flushed prefixed row must NOT carry an 8-col hanging indent "
            f"(double-indent regression); got {line!r}"
        )


@pytest.mark.asyncio
async def test_flushed_toplevel_tool_row_lands_at_col0() -> None:
    """Tier 2b: #1245 regression guard — a flushed TOP-LEVEL ToolCallRow lands
    at col 0, matching the live widget (CSS ``padding: 0 0``), NOT col 8.

    Exercises the real ``_do_flush_tool_call_row`` path. Before #1245 the
    top-level branch used ``_write_body`` → an 8-cell hanging-indent Padding
    the live row never had → the row jumped right at seal. Reverting to
    ``_write_body`` makes the flushed line start with 8 spaces → this FAILS.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        row = _row_no_prefix()
        conv.mount(row)
        await pilot.pause()
        conv._do_flush_tool_call_row(row)
        await pilot.pause()

        idx = _find_line_containing(log, "bash")
        line = _line_text(log, idx)
        # Live ToolCallRow renders at col 0; the flushed line must match.
        assert not line.startswith(" "), (
            f"flushed top-level tool row must start at col 0 (no leading "
            f"space, no 8-cell hanging indent); got {line!r}"
        )


@pytest.mark.asyncio
async def test_flushed_skill_row_lands_at_col0() -> None:
    """Tier 2b: #1245 regression guard — a finished SkillActivityRow flushes at
    col 0, matching the live widget (CSS ``padding: 0 0``), NOT col 8.

    Before #1245 ``finish_skill_row`` used ``_write_body`` → an 8-cell
    hanging-indent → the finished breadcrumb jumped right at seal. Reverting
    to ``_write_body`` makes the flushed line start with 8 spaces → FAILS.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv.start_skill_row("run-xyz", "code_review")
        await pilot.pause()
        conv.finish_skill_row("run-xyz", success=True)
        await pilot.pause()

        idx = _find_line_containing(log, "code_review")
        line = _line_text(log, idx)
        assert not line.startswith(" "), (
            f"flushed skill row must start at col 0 (no leading space); "
            f"got {line!r}"
        )
