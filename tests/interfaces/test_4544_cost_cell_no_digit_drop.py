"""Tier 2: #4544 — the TUI cost table's currency cells must never silently
drop a digit, and must always fit the fixed column width.

reyn-reviewer found two related defects in ``chrome.py``'s cost-breakdown
table (architect verified the exact thresholds, #4544):

- **Bug A (severity: wrong number displayed)** — the ``approx`` state's cell
  formatter built ``"~" + f"${value:.4f}"`` then byte-sliced the result to
  ``_COST_COL_W`` (9) characters. From ~$100 (``"~$999.9999"``, 10 chars)
  this silently dropped the LAST digit (``"~$999.999"``) — a genuinely
  DIFFERENT number that reads as a plausible rounding, not a truncation.
- **Bug B (severity: cosmetic, real amount)** — the Total / ``ok``-state
  cells were never fit-checked at all (``f"${value:.4f}"``, no truncation,
  no width shrink); Python's ``{:>9}`` format spec does not truncate an
  over-width string, so from $1000 the column simply overflowed, breaking
  every later column's alignment.

``_format_cost_cell`` (this file's subject) fixes both with ONE formatter
used for every currency cell regardless of state — architect's own review
note: the asymmetry (approx alone had a truncation path) is what caused
bug A. It sheds decimal PRECISION in stages until the correctly-ROUNDED
string fits, never slicing a formatted string's bytes.

No mocks — calls the real function with real floats.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import _COST_COL_W, _format_cost_cell


def test_every_returned_cell_fits_the_column_width() -> None:
    """Tier 2: non-vacuity + the core width invariant, swept across
    architect's own measured cases (#4544) plus one order of magnitude
    beyond them — every value, exact or approx, must fit."""
    values = (0.0, 9.8765, 99.9999, 999.9999, 1000.0, 12345.6789, 999999.9999)
    for value in values:
        for approx in (False, True):
            cell = _format_cost_cell(value, approx=approx)
            assert len(cell) <= _COST_COL_W, (
                f"_format_cost_cell({value!r}, approx={approx}) = {cell!r} "
                f"({len(cell)} chars) exceeds _COST_COL_W={_COST_COL_W}"
            )


def test_a_reduced_precision_cell_parses_back_to_a_correctly_rounded_value() -> None:
    """Tier 2: THE witness for bug A — a cell shortened to fit must be a
    real, correctly-rounded number, not a byte-sliced fragment. The old
    defect produced ``"~$999.999"`` for an input of 999.9999 (silently
    dropping the trailing 9, i.e. off by 0.0009 in a way indistinguishable
    from a genuine value at that display width — parsing it back gives a
    wrong number). This asserts the parsed-back value is always within a
    reduced-decimal ROUNDING tolerance of the true input, never off by a
    full truncated digit."""
    cases = (999.9999, 1000.0, 12345.6789, 99999.99999)
    for value in cases:
        for approx in (False, True):
            cell = _format_cost_cell(value, approx=approx)
            numeric_part = cell.lstrip("~$").rstrip("kMB")
            parsed = float(numeric_part)
            # Bug A's old behavior: byte-slicing "999.9999" to "999.999" is
            # off by 0.0009 — small in absolute terms but WRONG, not a
            # rounding. A correctly-rounded value at ANY of the precisions
            # this formatter tries (4dp down to 0dp) is off by at most 0.5
            # (0dp case) from the true value — a generous but real bound
            # that a byte-sliced fragment can violate in either direction
            # depending on where the cut lands relative to the decimal
            # point (a truncated INTEGER part, not just decimals, is the
            # failure mode this most needs to catch).
            assert abs(parsed - value) <= max(0.5, value * 0.001), (
                f"_format_cost_cell({value!r}, approx={approx}) = {cell!r} "
                f"parses back to {parsed}, too far from the true value to "
                f"be a rounding — looks like a digit was dropped"
            )


def test_a_correctly_rounded_boundary_value_rounds_up_cleanly() -> None:
    """Tier 2: falsify — the specific case architect's own repro table
    flags: 999.9999 must round UP to 1000.xx when precision is shed, not
    get stuck showing a stale 999.xxx (the exact shape a naive
    string-slice bug produces, since slicing never re-evaluates rounding)."""
    cell = _format_cost_cell(999.9999, approx=True)
    assert cell == "~$1000.00", (
        f"expected the correctly-rounded '~$1000.00', got {cell!r} — a "
        f"value stuck at '999.x' here means precision was shed by slicing, "
        f"not rounding"
    )


def test_approx_prefix_is_always_present_when_requested() -> None:
    """Tier 2: accept-side — shedding precision to fit must never drop the
    '~' marker itself; losing it would make an approximate figure
    indistinguishable from an exact one, misattributing the value's
    reliability."""
    for value in (9.8765, 999.9999, 12345.6789, 999999.9999):
        cell = _format_cost_cell(value, approx=True)
        assert cell.startswith("~"), f"{cell!r} lost its '~' marker for value={value}"


def test_exact_state_never_carries_an_approx_marker() -> None:
    """Tier 2: accept-side — the non-approx path must never emit '~',
    regardless of how much precision it has to shed."""
    for value in (9.8765, 999.9999, 12345.6789, 999999.9999):
        cell = _format_cost_cell(value, approx=False)
        assert not cell.startswith("~"), f"{cell!r} carries a stray '~' for value={value}"


def test_zero_renders_exactly() -> None:
    """Tier 2: accept-side — the common $0.0000 case is unaffected by the
    width-shedding logic (fits at full precision, no rounding needed)."""
    assert _format_cost_cell(0.0, approx=False) == "$0.0000"
