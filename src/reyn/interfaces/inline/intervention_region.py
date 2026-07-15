"""Intervention consumer of the inline region framework.

A closed-set intervention (confirm / select / grant-deny — anything with
``choices``) is hosted in the above-input region as a selectable list: one row
per choice, Enter delivers the chosen choice id authoritatively (via
``Session.answer_oldest_intervention_choice``, reusing the choice_id_override
path). Free-text interventions keep using the normal input field, so they get no
region element.

The region is poll-driven (like the status chips): the app reads the session's
head intervention each refresh and syncs the element — built here, never reaching
into prompt_toolkit so the mapping is unit-testable.
"""
from __future__ import annotations

import re
from typing import Callable

# Every choice label built by intervention_choices.py starts with a single
# bracketed hotkey letter — "[y]es" / "[A]lways" / "[n]o" / "[N]ever" / etc.
# The case there is REAL elsewhere: match_choice() (user_intervention.py) is
# explicitly case-sensitive, for the free-text/stdin surfaces (reyn run,
# cron, the plain --cui renderer) where a user actually types the letter.
# The inline picker never reads a typed hotkey at all — grepped: zero
# references to `.hotkey` anywhere under interfaces/inline/, selection here
# is cursor navigation + Enter only — so the bracket letter is pure
# decoration on THIS surface, and the upper/lower mix (one-shot vs
# persistent choices use different case) reads as visually inconsistent
# rather than meaningful. Normalize to lowercase for inline display only;
# the underlying label string (and every other renderer that uses it
# unmodified) is untouched.
_LEADING_HOTKEY_BRACKET = re.compile(r"^\[([A-Za-z])\]")


def _display_label(label: str) -> str:
    return _LEADING_HOTKEY_BRACKET.sub(lambda m: f"[{m.group(1).lower()}]", label, count=1)


class InterventionElement:
    """A RegionElement for one closed-set intervention.

    ``lines()`` are the choice labels (the selectable rows); ``on_select(row)``
    fires ``on_choose`` with that choice's id. ``iv_id`` ties the element to its
    intervention so the poll sync can tell when it has been resolved/replaced.
    """

    def __init__(
        self,
        iv_id: str,
        choices: list[tuple[str, str]],
        on_choose: Callable[[str, str], None],
    ) -> None:
        # choices: [(choice_id, label), ...]
        # #2770: choice labels are LLM-derived (ask_user options) and reach the
        # prompt_toolkit FormattedTextControl as raw f-string fragments (app.py)
        # + the scrollback echo, neither of which guards. Neutralize labels at
        # this data boundary through the SAME terminal neutralizer present's leaf
        # seam uses (ESC/control strip, FP-0054) so control/ESC sequences can't
        # drive the terminal. The choice_id (authoritative match key) is never
        # displayed and stays raw.
        from reyn.core.present.guard import get_neutralizer

        neut = get_neutralizer("terminal")
        self._iv_id = iv_id
        self._choices = [
            (cid, _display_label(neut.neutralize(label)[0])) for cid, label in choices
        ]
        self._on_choose = on_choose

    @property
    def iv_id(self) -> str:
        return self._iv_id

    def lines(self) -> list[str]:
        return [label for _, label in self._choices]

    def on_select(self, row: int) -> None:
        if 0 <= row < len(self._choices):
            choice_id, label = self._choices[row]
            self._on_choose(choice_id, label)


def build_intervention_element(iv, on_choose: Callable[[str, str], None]):
    """Build an :class:`InterventionElement` for a closed-set intervention, or
    None for a free-text one (no choices → handled by the input field).

    ``iv`` is a UserIntervention (read for ``id`` + ``choices``); kept duck-typed
    so the mapping is testable with a plain stand-in.
    """
    choices = getattr(iv, "choices", None) or []
    if not choices:
        return None
    rows = [(c.id, c.label) for c in choices]
    return InterventionElement(iv.id, rows, on_choose)
