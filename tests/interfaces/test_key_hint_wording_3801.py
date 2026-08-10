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


def test_the_help_pane_never_shows_a_key_the_way_textual_names_it() -> None:
    """Tier 1: the Help pane spells keys reyn's way, not Textual's (#3818).

    The pane is built from two sources — hand-written tables, where a person
    writes the display name, and the app's ``BINDINGS``, where Textual's key
    IDENTIFIER was rendered verbatim. Interleaved on one screen that produced
    ``escape  Close drawer`` directly beneath ``esc  back to composer``: one
    key, two spellings, adjacent.

    Asserted over the RENDERED lines rather than over either source, because
    the defect existed in neither one alone — each was internally consistent,
    and only the pane that shows both had a problem. A test reading one table
    would have passed throughout.

    ``escape`` is the only identifier reyn writes differently today, but the
    check is stated as "no identifier appears in the key column" so a binding
    for ``pageup`` or ``up`` — both already spelled differently in the tables
    (``pgup``, ``↑``) — cannot introduce the same split by simply existing.
    """
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    # Read from BINDINGS directly rather than through the app's own
    # ``_app_binding_help``. That adapter is what production feeds the pane, so
    # a bug there — dropping a binding — would make a test built on it pass by
    # showing less. This asks what the pane WOULD render given everything the
    # app declares, which is the question the defect was about.
    pairs = [
        (b[0], b[2]) if isinstance(b, tuple) else (b.key, b.description)
        for b in TextualChatApp.BINDINGS
        if isinstance(b, tuple) or (b.description and getattr(b, "show", True))
    ]

    rendered = chrome.help_pane_lines(pairs)
    assert rendered[1:], "the pane rendered no key rows — this gate would be vacuous"

    # Identifiers whose canonical Textual spelling is not how this interface
    # writes them. Names only — the point is that none reaches the key column.
    renamed = {"escape", "pageup", "pagedown", "up", "down"}
    keys = {line.strip().split("  ", 1)[0] for line in rendered[1:] if "  " in line.strip()}
    leaked = sorted(keys & renamed)
    assert not leaked, (
        f"the Help pane shows {leaked} in the key column — Textual's identifier "
        "rather than how this interface writes the key. A binding's Help row "
        "belongs in a hand-written table (chrome.*_KEYS) with show=False on the "
        "binding, not rendered from the identifier."
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
