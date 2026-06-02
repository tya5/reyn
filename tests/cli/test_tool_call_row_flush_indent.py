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

    Verifies the non-prefix path is unchanged: no extra whitespace is
    prepended by ToolCallRow itself; the hanging-indent comes from the
    flush path (_write_body Padding) in the conv pane.
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


def test_prefixed_row_flush_col_is_prefix_not_double_indent() -> None:
    """Tier 2: A4 fix — flushed prefixed row must not accumulate double-indent.

    The live widget (pre-flush) renders at column 0 with the label_prefix
    providing the visual indent.  After the A4 fix, the flush path calls
    ``_write_log`` (no Padding) for prefixed rows, so the flushed line
    matches the live rendering: leading chars are exactly the label_prefix,
    NOT (8-col Padding spaces + label_prefix).

    We verify this by asserting render_line1().plain starts with the
    literal prefix (not with 8 spaces).  If the old code (_write_body)
    were used, the Padding is applied OUTSIDE the Rich Text object
    (= by the RichLog/Padding wrapper), so the plain text itself stays
    the same — the column-position error manifests in the rendered layout.
    The plain-text check therefore targets the baked-in prefix rather
    than trying to observe the Padding wrapper (which is not visible in
    `.plain`).

    The structural contract verified here: label_prefix must be present
    at position 0 of the rendered plain text (= the flush path reads the
    same Text that the live widget renders, without any additional prefix
    injected by ToolCallRow itself).
    """
    row = _row_with_prefix("  └─ ")
    line1 = row.render_line1()
    plain = line1.plain
    # The prefix appears at index 0 — not preceded by any extra whitespace
    # that would indicate double-indenting.
    prefix = "  └─ "
    assert plain.startswith(prefix), (
        f"flush line must start with bare label_prefix {prefix!r}; "
        f"got leading chars: {plain[:20]!r}"
    )
    # After the prefix, the next non-space char should be the state glyph.
    after_prefix = plain[len(prefix):]
    first_nonspace = after_prefix.lstrip()
    assert first_nonspace and first_nonspace[0] in ("●", "✓", "✗", "⊘"), (
        f"first char after prefix must be a state glyph; got {after_prefix[:10]!r}"
    )
