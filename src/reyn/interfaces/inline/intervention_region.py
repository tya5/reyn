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
        on_choose: Callable[[str], None],
    ) -> None:
        # choices: [(choice_id, label), ...]
        self._iv_id = iv_id
        self._choices = list(choices)
        self._on_choose = on_choose

    @property
    def iv_id(self) -> str:
        return self._iv_id

    def lines(self) -> list[str]:
        return [label for _, label in self._choices]

    def on_select(self, row: int) -> None:
        if 0 <= row < len(self._choices):
            self._on_choose(self._choices[row][0])


def build_intervention_element(iv, on_choose: Callable[[str], None]):
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
