"""Tier 2: InputBar footer hint surfaces Ctrl+J newline (D-F8, wave-9).

Wave-9 Topic D finding F8 (P1): the only way to enter a multi-line
prompt is ``Ctrl+J``, but the always-visible footer hint advertised
only ``Enter send`` with no clue about newline entry. A first-time
user typing a code block or multi-step instruction hits Enter
expecting newline (= shell / chat-app muscle memory) and submits a
half-typed prompt. The fix surfaces ``Ctrl+J nl`` next to
``Enter send`` so the alternative is discoverable without opening
the Keys tab. ``Ctrl+P/N turn`` (= turn navigation power-user
convenience) is dropped from the footer to stay under the 72-cell
budget.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _hint() -> str:
    from reyn.interfaces.tui.widgets.input_bar import InputBar
    # ``_build_hint`` is a pure render that doesn't read any state — call
    # it on a bare instance without going through Textual mount lifecycle.
    bar = InputBar.__new__(InputBar)
    return InputBar._build_hint(bar)


def test_hint_surfaces_ctrl_j_newline_next_to_enter_send() -> None:
    """Tier 2: ``Ctrl+J nl`` appears adjacent to ``Enter send``.

    Adjacency matters — the user reads the footer left-to-right and
    pairs ``Enter`` with the very next key as "if not Enter, then…".
    Placing ``Ctrl+J nl`` directly after ``Enter send`` makes the
    multi-line affordance obvious.
    """
    hint = _hint()
    assert "Enter send" in hint
    assert "Ctrl+J nl" in hint
    # The two must appear in this order with nothing else between them
    # except the separator.
    assert hint.index("Enter send") < hint.index("Ctrl+J nl")
    between = hint[
        hint.index("Enter send") + len("Enter send") : hint.index("Ctrl+J nl")
    ]
    assert "│" in between
    assert "Ctrl+" not in between, (
        f"another Ctrl+ key snuck between Enter send and Ctrl+J nl: {between!r}"
    )


def test_hint_fits_72_cell_budget_for_default_80_col_terminal() -> None:
    """Tier 2: hint stays within the 72-cell footer budget.

    Default 80-col terminal minus 8 cells of conv-pane chrome leaves
    ~72 cells for the footer. Going over wraps the trailing key to
    a second line as an orphan hint.
    """
    width = cell_len(_hint())
    assert width <= 72, f"footer hint is {width} cells, must be ≤72"


def test_hint_retains_essential_keys() -> None:
    """Tier 2: dropping ``Ctrl+P/N turn`` does not also drop critical keys.

    The trade-off is explicit: ``Ctrl+P/N turn`` is a power-user
    convenience (= jumping between past turns), ``Ctrl+J nl`` is a
    first-encounter need. Cancel / clear / panel-toggle remain.
    """
    hint = _hint()
    assert "Ctrl+C cancel" in hint
    assert "Ctrl+L clear" in hint
    assert "Ctrl+B panel" in hint
    # Power-user keys belong to the Keys tab now, not the footer.
    assert "Ctrl+P/N" not in hint


def test_hint_uses_box_drawing_separator_consistently() -> None:
    """Tier 2: every key is separated by the same ``│`` glyph.

    Mixing separators (e.g. ``│`` + ``·`` + ``,``) would visually
    break the footer rhythm.
    """
    hint = _hint()
    # 5 keys → 4 separators.
    assert hint.count("│") == 4, f"expected 4 separators, got {hint.count('│')}"
