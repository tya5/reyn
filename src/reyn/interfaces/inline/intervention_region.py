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

from typing import Callable

# NOTE (#2943 review): an earlier revision of this file lowercased the
# leading [X] hotkey bracket for inline display, on the theory that the
# picker widget never reads a typed hotkey (true — zero `.hotkey` references
# under interfaces/inline/, selection here is cursor+Enter only) so the
# case was pure decoration. That was WRONG: the input bar BELOW this same
# picker, while an intervention is pending, ALSO accepts a typed answer
# (app.py's plain-text submit path -> session.py's ask_user input handling
# -> match_choice(), user_intervention.py — explicitly case-sensitive). A
# user reads the picker's label and can type the letter into that bar as an
# alternative to arrow+Enter; the two are the same surface from the user's
# perspective. The case difference is not visual noise — it IS the hotkey:
# "[y]es"/"[n]o" are one-shot (lowercase), "[A]lways"/"[N]ever" are
# persistent (uppercase). Normalizing case would make "[a]lways" advertise a
# letter that doesn't match anything (real hotkey is "A"), and worse, a user
# meaning to permanently decline via "[n]ever" who types "n" would silently
# get the one-shot "[n]o" instead — a false sense of having set a permanent
# deny. Labels are therefore rendered EXACTLY as `intervention_choices.py`
# defines them; no casing transform of any kind happens in this module.


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
        self._choices = [(cid, neut.neutralize(label)[0]) for cid, label in choices]
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
