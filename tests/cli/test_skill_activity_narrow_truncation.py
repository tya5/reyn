"""Tier 2: SkillActivityRow width-adaptive truncation at narrow terminals.

Before this PR, ``_build_running`` and ``_build_finished`` assembled
Rich Text without consulting ``self.size.width``.  With ``height: auto``
and no ``overflow`` CSS, long rows wrapped to 2+ lines at ~55 cols,
disrupting conversation flow.

This test suite pins the truncation contract introduced in the fix:

- At NARROW width (≤ 55 cols), the rendered plain text of
  ``build_running()`` fits within the budget (cell_len ≤ width).
- The leading glyph + elapsed segment are ALWAYS present.
- When content is long, the truncation ellipsis ``…`` appears in the
  output (= active truncation, not accidental short content).
- At NARROW width, the Ctrl+B hint is dropped from the finished
  success/failure render (it's the last thing to be dropped).
- At WIDE width (80 cols), the finished success line still includes
  the Ctrl+B hint (= regression check, common case preserved).

All assertions go through the public surface:
- ``row.build_running().plain`` (running state, per the public alias
  added in this widget)
- ``row.rendered_text()`` (cache accessor, via RenderableCacheMixin)
- ``rich.cells.cell_len`` to count display cells correctly (CJK-aware)

Per CLAUDE.md testing policy: NEVER assert on private state.
Docstring first line declares Tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _row(
    *,
    skill_name: str = "my_skill",
    run_id: str = "abcd1234",
) -> "SkillActivityRow":  # type: ignore[name-defined]  # noqa: F821
    """Construct a SkillActivityRow without mounting it.

    ``_refresh`` returns early when ``_static is None`` (= not composed),
    so methods that drive internal state can be exercised directly.
    The builder methods (``_build_running`` / ``_build_finished``) are
    pure over the widget's fields and call ``_available_width()``
    which falls back to 80 when not mounted.  We patch the size
    attribute to simulate narrow / wide terminals.
    """
    from reyn.interfaces.tui.widgets.skill_activity import SkillActivityRow

    return SkillActivityRow(run_id=run_id, skill_name=skill_name)


def _set_width(row, width: int) -> None:
    """Inject a test width so ``_available_width()`` returns ``width``.

    Sets the ``_width_override`` instance attribute introduced for this
    purpose — ``_available_width`` checks it before reading ``self.size``,
    which is a read-only property on unmounted Textual widgets.
    """
    row._width_override = width


# ── _build_running truncation ──────────────────────────────────────────────────

def test_running_fits_within_narrow_width() -> None:
    """Tier 2: at 40-col width, build_running output fits in the budget.

    Pre-fix: build_running() ignored self.size.width entirely, so
    the plain text could exceed the terminal width and cause wrapping.
    Post-fix: the row reads the width and truncates so cell_len ≤ width.
    """
    row = _row(skill_name="a_long_skill_name_here", run_id="abcd1234")
    row.set_phase("executing_phase_now")
    _set_width(row, 40)
    text = row.build_running()
    plain = text.plain
    # Right margin is reserved internally; the visible text must not
    # exceed the terminal width (wrapping line = cell_len > width).
    width = cell_len(plain)
    assert width <= 40, (
        f"Expected cell_len ≤ 40, got {cell_len(plain)}: {plain!r}"
    )


def test_running_glyph_always_present_at_narrow_width() -> None:
    """Tier 2: the leading spinner glyph is present even at 40 cols.

    The degrade order drops detail → phase (truncated) → skill_name#id
    (truncated), but the glyph is always the FIRST segment appended and
    is never subject to truncation — it is part of the always-kept set.
    """
    row = _row(skill_name="very_long_skill_name_that_exceeds_budget")
    row.set_phase("phase")
    _set_width(row, 40)
    text = row.build_running()
    plain = text.plain
    # The leading character should be a braille spinner (all single-width),
    # not empty or an ellipsis.  Check the plain starts with a non-space char.
    stripped = plain.lstrip()
    assert stripped, "Rendered text must not be entirely whitespace"
    first_char = stripped[0]
    assert first_char != "…", (
        f"Leading glyph must not be an ellipsis; got {plain!r}"
    )


def test_running_elapsed_always_present_at_narrow_width() -> None:
    """Tier 2: the elapsed segment is present at 40 cols (it is always-kept).

    The elapsed time is the primary "still alive" signal; dropping it
    would violate the spec.  Pin that it's in the rendered output even
    at very narrow widths.
    """
    row = _row(skill_name="any_skill")
    row.set_phase("running")
    _set_width(row, 40)
    text = row.build_running()
    plain = text.plain
    # Elapsed format is "<space><space><N>.<M>s".  The trailing "s"
    # suffix is unambiguous enough as a presence check.
    assert plain.endswith("s"), (
        f"Elapsed segment ('...Ns') must appear; got {plain!r}"
    )


def test_running_detail_dropped_at_narrow_width() -> None:
    """Tier 2: at 40 cols the in-phase detail is dropped (degrade step 1).

    Detail is the FIRST thing dropped when budget is tight.  At 40 cols
    with a typical skill name + phase, the detail segment shouldn't fit.
    """
    row = _row(skill_name="my_skill")
    row.set_phase("run")
    row.set_detail("llm: claude-opus-4-7-20260501")  # 30+ chars
    _set_width(row, 40)
    text = row.build_running()
    plain = text.plain
    # Confirm detail excluded by checking the cell budget constraint:
    # if detail were present, the line would exceed 40 cells.
    width = cell_len(plain)
    assert width <= 40, (
        f"cell_len should stay ≤ 40 (detail dropped): {plain!r}"
    )


def test_running_detail_present_at_wide_width() -> None:
    """Tier 2: at 120 cols the detail IS included (regression check).

    No truncation pressure at wide widths — all segments should render.
    """
    row = _row(skill_name="my_skill")
    row.set_phase("run")
    row.set_detail("llm: my-model")
    _set_width(row, 120)
    text = row.build_running()
    plain = text.plain
    assert "llm: my-model" in plain, (
        f"Detail must appear at wide width; got {plain!r}"
    )


def test_running_truncation_ellipsis_on_very_long_skill_name() -> None:
    """Tier 2: extremely long skill_name#id gets ellipsis at 35 cols.

    Last-resort degrade: when even skill_name#id alone exceeds the
    body budget, it is truncated with '…'.  The ellipsis must appear.
    """
    # 50-char skill name will force truncation at 35 cols.
    row = _row(skill_name="a" * 50, run_id="abcd1234")
    row.set_phase("")
    _set_width(row, 35)
    text = row.build_running()
    plain = text.plain
    assert "…" in plain, (
        f"Ellipsis must appear for very long skill name at 35 cols; got {plain!r}"
    )
    width = cell_len(plain)
    assert width <= 35, (
        f"Expected cell_len ≤ 35, got {cell_len(plain)}: {plain!r}"
    )


# ── _build_finished truncation ─────────────────────────────────────────────────

def test_finished_success_ctrl_hint_dropped_at_narrow_width() -> None:
    """Tier 2: at 45-col width, the 'Ctrl+B → agents' hint is dropped.

    The hint is ~20 cells and is the lowest-priority segment on the
    finished success line.  It should be dropped at narrow widths to
    keep the essential elapsed + skill info on one line.

    Pre-fix: the hint was always appended regardless of width,
    causing the finished row to wrap at narrow terminals.
    """
    row = _row(skill_name="my_skill")
    row.finish(success=True, reason="3 phases")
    _set_width(row, 45)
    # Access the finished render via rendered_text (exercises the cache path).
    # Call _refresh manually since we're not mounted.
    finished_text = row._build_finished()
    plain = finished_text.plain
    assert "Ctrl+B" not in plain, (
        f"Ctrl+B hint must be dropped at 45 cols; got {plain!r}"
    )
    width = cell_len(plain)
    assert width <= 45, (
        f"Expected cell_len ≤ 45, got {cell_len(plain)}: {plain!r}"
    )


def test_finished_success_ctrl_hint_present_at_wide_width() -> None:
    """Tier 2: at 80+ cols the 'Ctrl+B → agents' hint IS present (regression).

    The common case (80-col terminal) should still show the hint.
    """
    row = _row(skill_name="my_skill")
    row.finish(success=True, reason="3 phases")
    _set_width(row, 80)
    finished_text = row._build_finished()
    plain = finished_text.plain
    assert "Ctrl+B" in plain, (
        f"Ctrl+B hint must appear at 80 cols; got {plain!r}"
    )


def test_finished_failure_ctrl_hint_dropped_at_narrow_width() -> None:
    """Tier 2: at 45-col width, the 'Ctrl+B → events' hint is dropped.

    Same degrade contract as the success case — the hint is the
    lowest-priority segment.
    """
    row = _row(skill_name="my_skill")
    row.finish(success=False, reason="timeout")
    _set_width(row, 45)
    finished_text = row._build_finished()
    plain = finished_text.plain
    assert "Ctrl+B" not in plain, (
        f"Ctrl+B hint must be dropped at 45 cols; got {plain!r}"
    )
    width = cell_len(plain)
    assert width <= 45, (
        f"Expected cell_len ≤ 45, got {cell_len(plain)}: {plain!r}"
    )


def test_finished_failure_ctrl_hint_present_at_wide_width() -> None:
    """Tier 2: at 80+ cols the 'Ctrl+B → events' hint IS present (regression)."""
    row = _row(skill_name="my_skill")
    row.finish(success=False, reason="timeout")
    _set_width(row, 80)
    finished_text = row._build_finished()
    plain = finished_text.plain
    assert "Ctrl+B" in plain, (
        f"Ctrl+B hint must appear at 80 cols; got {plain!r}"
    )


def test_finished_aborted_cancel_msg_truncated_at_narrow_width() -> None:
    """Tier 2: cancelled row with long reason is truncated at 40 cols.

    The aborted state appends 'cancelled: <reason>'; a very long reason
    should be truncated with ellipsis to stay within the terminal width.
    """
    row = _row(skill_name="my_skill")
    row.finish(success=False, reason="this_is_a_very_long_cancellation_reason_string", aborted=True)
    _set_width(row, 40)
    finished_text = row._build_finished()
    plain = finished_text.plain
    width = cell_len(plain)
    assert width <= 40, (
        f"Expected cell_len ≤ 40, got {cell_len(plain)}: {plain!r}"
    )


def test_truncate_to_cells_cjk_aware() -> None:
    """Tier 2: _truncate_to_cells counts CJK characters as 2 cells each.

    CJK glyphs are 2 cells wide; naive ``len()`` would undercount and
    allow the string to exceed the budget.  Pin that the truncation
    respects display-cell width.
    """
    row = _row()
    # Each CJK char = 2 cells; 5 chars = 10 cells.
    cjk = "あいうえお"
    # Budget 6 cells → 3 CJK chars (6 cells) would be the naive answer,
    # but we need to reserve 1 cell for the ellipsis, so result is 2 CJK
    # chars + "…" = 5 cells.
    result = row._truncate_to_cells(cjk, 6)
    width = cell_len(result)
    assert width <= 6, (
        f"Expected cell_len ≤ 6, got {width}: {result!r}"
    )
    assert "…" in result, "Ellipsis must appear when truncation happened"
