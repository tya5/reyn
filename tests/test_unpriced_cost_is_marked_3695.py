"""#3695 display side — a cost that cannot be complete says so.

The recording side (#3701) stopped conflating "no published price" with
"free": an unpriced call now increments a counter instead of vanishing into a
0.00 that the total absorbs silently. Nothing read that counter, so the owner
still saw the same frozen figure — a mechanism that exists and is never
reached, which is the shape this arc keeps producing.

Two surfaces, sized to the room each has:

- the status row is the ONE always-visible chrome region, and #3326 packs it
  onto the menu row only while it fits. It gets a two-cell mark, so the caveat
  never costs a row of conversation.
- the Cost pane has room for the sentence, and carries the count.

The gates are about the DISTINCTION, not the glyph: a priced session must not
be marked, an unpriced one must be, and the pane's line must be absent rather
than showing a zero. The marker itself is one constant (``UNPRICED_MARK``) —
these tests read it rather than spelling it, so changing the look is a
one-line edit and not a test rewrite.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    UNPRICED_MARK,
    cost_figure,
    cost_pane_lines,
    pack_menu_rows,
    status_fits_last_row,
    status_line_text,
)

_AGENT = "default"


def _snap(*, cost: float = 0.1234, unpriced: int = 0) -> dict:
    return {
        "model_active_class": "some-model",
        "attached_name": _AGENT,
        "cost_agent": cost,
        "cost_agent_unpriced_calls": unpriced,
        "usage": (100, 50, 150),
        "agent_tokens": 150,
    }


def test_a_fully_priced_cost_carries_no_caveat() -> None:
    """Tier 2: the ordinary case is untouched.

    A mark that appeared on every session would stop meaning anything, and
    would be read as decoration the first time it was wrong.
    """
    assert cost_figure(_snap()) == "$0.1234"
    assert UNPRICED_MARK not in status_line_text(_snap(), _AGENT)


def test_an_unpriced_call_marks_the_figure() -> None:
    """Tier 2: with any unpriced call, the figure is marked as a lower bound.

    This is the owner's case: a model absent from the bundled cost map, a
    figure that never moves, and nothing anywhere saying why.
    """
    marked = cost_figure(_snap(unpriced=3))

    assert marked.startswith("$0.1234")
    assert marked.endswith(UNPRICED_MARK)
    assert UNPRICED_MARK in status_line_text(_snap(unpriced=3), _AGENT)


def test_the_mark_survives_a_zero_total() -> None:
    """Tier 2: $0.0000 with unpriced calls is the most misleading case of all.

    "Free" and "we have no idea" render identically without the mark, and this
    is exactly the state a session that only ever used an unpriced model is in.
    """
    assert cost_figure(_snap(cost=0.0, unpriced=7)).endswith(UNPRICED_MARK)


def test_the_cost_pane_explains_the_mark_and_counts_it() -> None:
    """Tier 2: the pane says what the status row only had room to hint."""
    lines = cost_pane_lines(_snap(unpriced=4))

    explanation = [line for line in lines if "unpriced" in line]
    assert explanation, f"the pane does not mention the gap: {lines}"
    assert "4" in explanation[0]
    assert "lower bound" in explanation[0]


def test_the_cost_pane_says_nothing_when_every_call_was_priced() -> None:
    """Tier 2: no line rather than a zero.

    A permanent "unpriced 0" is a line every reader has to look at and decide
    is irrelevant.
    """
    assert not [line for line in cost_pane_lines(_snap()) if "unpriced" in line]


def test_the_mark_stays_short_enough_not_to_cost_a_chrome_row() -> None:
    """Tier 2: the caveat must not push the status row off the menu row.

    #3326 merges the status segment onto the menu row only while it fits, and
    #3668 measured what happens when something makes that line longer: at 80
    columns the segment moves to a row of its own — a row of conversation,
    permanently. The mark is bounded so the caveat cannot buy that.
    """
    from reyn.interfaces.inline.textual_chat.app import _MENU_TABS

    content_width = 78  # an 80-column terminal, less the row's own padding
    rows = pack_menu_rows(_MENU_TABS, content_width)
    fits = lambda line: status_fits_last_row(rows, content_width, len(line))

    assert fits(status_line_text(_snap(unpriced=1), _AGENT)) == fits(
        status_line_text(_snap(), _AGENT)
    ), (
        "the unpriced mark changed whether the status segment shares the menu "
        "row at 80 columns — that is a row of conversation, permanently, "
        "bought by a caveat"
    )
