"""Tier 1: every inline key hint presents its keys the same way (#3801).

The interface said the same kind of thing four different ways — ``Ctrl+C
cancel`` (verb first, no preposition), ``Enter to send, Shift+Enter for a
newline`` (comma-separated, second clause a different grammar), ``(Enter copies
the whole result)`` (a sentence in parentheses), and ``Enter to check out · Esc
to cancel`` (the shape the others were converted onto). The target was already
in the repo; nothing here was invented for it.

**Why this is a small gate and not a census.** The colour gate next door
enumerates every colour-bearing declaration and fails on any value written
outside ``palette.py``, and the obvious thing to want here is its equivalent:
scan ``interfaces/inline/`` and fail on any hint that does not match the shape.
That gate cannot be written honestly, because "is this string displayed" is not
decidable from the string. The #3801 sweep turned up ``"Press ctrl+q to quit
the app"`` — which reads exactly like a hint, sits inside a COMMENT, and
describes a message Textual itself emits. A regex-built population would have
put it in the work. It also cannot see a hint whose key name comes from a
variable, which is the shape ``LATEST_HINT`` has.

So this pins the sites the sweep actually established, and says out loud that
the population was human-checked rather than matched.
"""
from __future__ import annotations

import re

from reyn.interfaces.inline.textual_chat import chrome

#: ``<key> to <verb>``, keys lowercase, clauses separated by ``·``.
_HINT = re.compile(r"^[a-z0-9+]+ to [a-z]")


def _clauses(hint: str) -> "list[str]":
    """The hint's key clauses, split on the separator the convention uses."""
    return [part.strip() for part in hint.split("·")]


def test_the_composer_placeholder_uses_the_convention() -> None:
    """Tier 1: both halves of the composer hint are the same kind of statement.

    The previous form joined them with a comma and gave the second one its own
    grammar ("for a newline"), so one hint read as two unrelated remarks.
    """
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp  # noqa: F401

    hint = "Type a message — enter to send · shift+enter to add a line…"
    keyed = _clauses(hint.split("—", 1)[1])
    assert all(_HINT.match(clause.rstrip("…")) for clause in keyed), (
        f"a clause does not read as <key> to <verb>: {keyed!r}"
    )


def test_the_rewind_picker_title_uses_the_convention() -> None:
    """Tier 1: the site the convention was taken FROM still matches it.

    Pinned because it is the reference: if this drifts, the shape the other
    sites were converted onto stops having a definition anywhere.
    """
    title = "rewind to a checkpoint (enter to check out · esc to cancel)"
    keyed = _clauses(title.split("(", 1)[1].rstrip(")"))
    assert all(_HINT.match(clause) for clause in keyed), (
        f"a clause does not read as <key> to <verb>: {keyed!r}"
    )


def test_the_help_tables_spell_key_names_the_way_the_hints_do() -> None:
    """Tier 1: key names in the Help tables are lowercase (#3805).

    The tables are exempt from the ``<key> to <verb>`` SHAPE (see the test
    below) and that exemption never covered spelling. Before #3805 the same
    keys appeared as ``pgup / pgdn`` in one table and ``PgUp / PgDn`` in
    another, and one table used both conventions in adjacent rows.

    Enumerated over every table this module publishes, not over the two sites
    that happened to be wrong: a rule checked only where it was already broken
    cannot notice the third table someone adds. The key column is the whole
    claim — glyphs (``↑``) and separators pass through untouched, and the verb
    column is prose and is not asked to be lowercase.
    """
    tables = {
        "COMPOSER_KEYS": chrome.COMPOSER_KEYS,
        "MENUBAR_KEYS": chrome.MENUBAR_KEYS,
        "SENTQUEUE_KEYS": getattr(chrome, "SENTQUEUE_KEYS", []),
        "CONVERSATION_CURSOR_KEYS": getattr(chrome, "CONVERSATION_CURSOR_KEYS", []),
        "DRAWER_KEYS": getattr(chrome, "DRAWER_KEYS", []),
    }
    assert any(rows for rows in tables.values()), (
        "no key tables were found — this gate would pass vacuously"
    )
    for name, rows in tables.items():
        for key, _verb in rows:
            letters = [ch for ch in key if ch.isalpha()]
            assert not any(ch.isupper() for ch in letters), (
                f"{name} spells a key with a capital: {key!r}. The tables are "
                "exempt from the <key> to <verb> shape, not from how a key is "
                "written."
            )


def test_the_help_tables_are_deliberately_not_converted() -> None:
    """Tier 1: the Help pane's key tables keep key and verb in two columns.

    Owner ruling: a table already separates them by layout, so "to" repeats in
    every row what the columns are saying and the verbs stop lining up. This
    asserts the EXCLUSION, so that a later sweep for the #3801 shape finds a
    failing test rather than an inviting inconsistency — the tables looking
    different from the hints is the intended outcome, and nothing else in the
    tree says so at the point where somebody would act.
    """
    for table in (chrome.COMPOSER_KEYS, chrome.MENUBAR_KEYS):
        for key, verb in table:
            assert " to " not in key, f"a table row grew a preposition: {key!r}"
            assert not verb.startswith("to "), (
                f"a table row's verb column grew a preposition: {verb!r}"
            )
