"""Tier 1: ``DrawerRow``'s slots are the row's contract (#3691 Phase 2).

The four capability panes (tool / mcp / skill / hook) each assembled their rows
by hand and each spelled the same grammar out again — a state mark, the name, an
optional ``·``-separated note, a slash command. #3380 added the denial-reason
distinction and #3615 a third reason; both are changes to how the mark is
decided, and the mark was decided in more than one place.

What this file pins is the TYPE's contract, not the pixels. The rendering is
byte-identical to what the four builders produced (verified by comparing before
and after on the same snapshot — recorded on the PR rather than landed here,
since a byte-identity check for a completed extraction has nothing left to
protect once the extraction is in).

The load-bearing one is ``command``: a denied capability and an operable one
must not be the same kind of thing. Previously both were strings and "inert" was
spelled ``""``, so telling them apart was the caller's job every time.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    DrawerRow,
    pane_commands,
    pane_payload,
)

_DENIED_SNAP = {
    "visibility_items": [
        {"kind": "tool", "name": "read_file", "on": True, "denied": False,
         "denied_reason": None},
        {"kind": "tool", "name": "shell", "on": False, "denied": True,
         "denied_reason": "envelope"},
        {"kind": "tool", "name": "web_fetch", "on": False, "denied": True,
         "denied_reason": "turn_context"},
        {"kind": "tool", "name": "run_pipeline", "on": False, "denied": True,
         "denied_reason": "unknown"},
    ]
}


def test_a_row_with_no_command_is_inert() -> None:
    """Tier 1: ``command=None`` is the type saying "not operable"; the registry
    boundary still spells that ``""`` because ``pane_commands`` hands out
    strings."""
    assert DrawerRow(label="shell", state="--").as_entry()[1] == ""
    assert DrawerRow(label="x", command="/visibility on tool x").as_entry()[1] == (
        "/visibility on tool x"
    )


def test_the_state_mark_is_a_slot_not_a_prefix_in_the_label() -> None:
    """Tier 1: the mark is composed, so a row without a state has no brackets at
    all — the read-only fallback listings depend on that."""
    assert DrawerRow(label="github", state="on").text == "[on] github"
    assert DrawerRow(label="github").text == "github"


def test_the_note_is_appended_with_the_separator_the_panes_share() -> None:
    """Tier 1: one place decides the ``·`` separator, so a pane cannot drift into
    its own spelling of it."""
    assert DrawerRow(label="h", state="on", note="turn_end").text == (
        "[on] h  · turn_end"
    )
    assert DrawerRow(label="h", state="on").text == "[on] h"


def test_every_denied_capability_is_unoperable_and_says_why() -> None:
    """Tier 2: through the real pane, not the type: each denial reason reaches
    the row as its OWN sentence, and no denied row carries a command.

    The reasons differ in what the operator does next — edit a profile, wait for
    the context to clear, or neither because authorization could not be read at
    all (#3380, #3615). A refactor that collapsed them would still render three
    plausible rows.
    """
    rows = pane_payload("tool", snapshot=_DENIED_SNAP)
    cmds = pane_commands("tool", _DENIED_SNAP)

    denied = {r: c for r, c in zip(rows, cmds) if r.startswith("[--]")}
    # Which capabilities were refused, by name — the snapshot's three denials.
    assert sorted(r.split()[1] for r in denied) == ["run_pipeline", "shell", "web_fetch"]
    assert set(denied.values()) == {""}, (
        f"a denied capability was handed an operable command: {denied}"
    )
    # Three reasons, three sentences: the operator's next move differs per reason,
    # so a collapse here would still render three plausible-looking rows.
    notes = {r.split("· ", 1)[1] for r in denied}
    assert sorted(notes) == sorted(
        {
            "denied by capability profile",
            "denied while untrusted content is in context",
            "authorization could not be determined for this session",
        }
    ), f"the denial reasons are not distinct: {notes}"


def test_an_operable_row_keeps_its_toggle() -> None:
    """Tier 2: the extraction did not cost the panes what they are for."""
    rows = pane_payload("tool", snapshot=_DENIED_SNAP)
    cmds = pane_commands("tool", _DENIED_SNAP)
    operable = {r: c for r, c in zip(rows, cmds) if c}
    assert operable == {"[on] read_file": "/visibility off tool read_file"}
