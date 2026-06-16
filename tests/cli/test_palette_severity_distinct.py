"""Tier 2: ErrorBox severity-ramp tokens must stay pairwise distinct.

Sibling of test_palette_semantic_distinctions.py, scoped to the ErrorBox
severity ramp (_SEV_*). Kept in a separate file so this PR does not collide
with the in-flight Phase-2b edits to the shared distinctions test.

These assertions pin DISTINCTNESS (inequality of string values), not the
specific hex — they are NOT format-pins. A "merge the reds" cleanup that
collapsed any of these would silently erase a designed visual distinction
(CI-invisible regression otherwise).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui._palette import (
    _SEV_HIGH,
    _SEV_HIGH_HOVER,
    _SEV_MED,
    _SEV_MED_HOVER,
    _STATUS_ERROR,
)


def test_sev_high_distinct_from_sev_med() -> None:
    """Tier 2: HIGH vs MED severity tiers must stay visually separable.

    The ErrorBox border + header colour encodes severity: HIGH (red) is a
    halt-and-read signal, MED (amber) a recoverable/transient one. If the two
    tiers collapse to the same colour the operator loses the at-a-glance
    triage cue the 3-tier ramp exists to provide.
    """
    assert _SEV_HIGH != _SEV_MED, (
        f"_SEV_HIGH and _SEV_MED must be distinct severity tiers; "
        f"got both = {_SEV_HIGH!r}."
    )


def test_sev_hover_tiers_distinct() -> None:
    """Tier 2: HIGH-hover and MED-hover must mirror the rest-state distinction."""
    assert _SEV_HIGH_HOVER != _SEV_MED_HOVER, (
        f"_SEV_HIGH_HOVER and _SEV_MED_HOVER must be distinct; "
        f"got both = {_SEV_HIGH_HOVER!r}."
    )


def test_sev_high_distinct_from_status_error() -> None:
    """Tier 2: the ErrorBox-card severity red is distinct from the event-failure red.

    _SEV_HIGH (#cc5555, the ErrorBox card's HIGH-severity border/header) and
    _STATUS_ERROR (#ff6644, the events-tab recoverable-failure colour) are
    different concepts on different surfaces. They are intentionally separate
    tokens — neither is the canonical "red"; collapsing them would couple two
    unrelated surfaces' theming.
    """
    assert _SEV_HIGH != _STATUS_ERROR, (
        f"_SEV_HIGH (ErrorBox severity) and _STATUS_ERROR (event failure) must "
        f"stay distinct tokens; got _SEV_HIGH={_SEV_HIGH!r}, "
        f"_STATUS_ERROR={_STATUS_ERROR!r}."
    )
