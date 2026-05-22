"""Tier 2: ``suggest_for_unknown`` contract for the unknown-slash ErrorBox.

Wave-6 SL2 — when a user types ``/typo``, the ErrorBox previously dumped
the full 20+ command catalog into a 72-cell header which truncated the
list mid-name (= ``try: /agent, /agents, /answer, /attach,`` cut off
mid-suggestion). The new helper produces a tight 3-fuzzy-match list
with ``/help`` always appended as the escape hatch.

Pins:

1. ``get_close_matches`` style fuzzy match returns 3 results when at
   least 3 names are reasonably similar.
2. When NO command is close enough, fall back to the alphabetical
   head (= 3 names) so the user still sees a suggestion shape.
3. ``help`` is always present in the output, even when it was not in
   the fuzzy matches.
4. ``help`` is NOT duplicated when the fuzzy match already includes it.
5. The rendered ``"try: /a, /b, /c, /help"`` string for the common
   typo shapes stays under the 72-cell ErrorBox header cap so the
   line doesn't truncate mid-suggestion. Edge cases (= very long
   user-typed typo strings) may overflow by a few cells; that's
   accepted as the typo length itself dominates the budget.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.slash import suggest_for_unknown


# Canonical names list used across tests so the assertions stay stable
# even as new slash commands land in the real registry.
_FIXTURE_NAMES = sorted([
    "agent", "agents", "answer", "attach", "budget", "cancel",
    "copy", "cost", "cost-inline", "docs-filter", "exit", "expand",
    "help", "image", "list", "memory", "pending", "plan", "quit",
    "restore", "tasks", "zen",
])


def test_fuzzy_match_returns_three_close_matches() -> None:
    """Tier 2: typo close to an existing name → fuzzy match returns it."""
    suggestions = suggest_for_unknown("cope", names=_FIXTURE_NAMES)

    # ``copy`` and ``cost`` are within fuzzy similarity of ``cope``.
    assert "copy" in suggestions
    assert "cost" in suggestions
    # ``help`` is always present.
    assert "help" in suggestions
    # Cap at 4 = 3 fuzzy + always-on /help.
    assert len(suggestions) <= 4


def test_no_match_falls_back_to_alphabetical_head() -> None:
    """Tier 2: typo with no close match → falls back to the first 3 names."""
    # A nonsense token that has no fuzzy similarity to any registry name.
    suggestions = suggest_for_unknown("xxxxxxxxxxxxxxx", names=_FIXTURE_NAMES)

    # Falls back to the alphabetical head (= ``agent``, ``agents``,
    # ``answer``) plus the always-appended ``help``.
    assert suggestions[:3] == _FIXTURE_NAMES[:3]
    assert "help" in suggestions


def test_help_always_present() -> None:
    """Tier 2: ``help`` is always in suggestions even when fuzzy missed it."""
    suggestions = suggest_for_unknown("cope", names=_FIXTURE_NAMES)
    assert "help" in suggestions


def test_help_not_duplicated_when_fuzzy_picks_it() -> None:
    """Tier 2: typo close to ``help`` → ``help`` appears once, not twice."""
    suggestions = suggest_for_unknown("hel", names=_FIXTURE_NAMES)
    assert suggestions.count("help") == 1


def test_common_typo_message_fits_in_error_box_header() -> None:
    """Tier 2: rendered message stays under the 72-cell ErrorBox cap.

    Pins the load-bearing UX guarantee — the previous full-catalog
    dump truncated mid-suggestion at the 72-cell boundary. After this
    fix common typo lengths stay safely below.
    """
    common_typos = ["typo", "qit", "lis", "hel", "cope", "agen", "co"]
    for typo in common_typos:
        suggestions = suggest_for_unknown(typo, names=_FIXTURE_NAMES)
        known = ", ".join(f"/{n}" for n in suggestions)
        msg = f"unknown command /{typo}; try: {known}"
        assert len(msg) <= 72, (
            f"common typo /{typo!s} would overflow ErrorBox header "
            f"({len(msg)} cells): {msg!r}"
        )


def test_empty_command_uses_alphabetical_head() -> None:
    """Tier 2: empty ``cmd`` (= bare ``/``) → alphabetical head + /help."""
    suggestions = suggest_for_unknown("", names=_FIXTURE_NAMES)
    assert suggestions[:3] == _FIXTURE_NAMES[:3]
    assert "help" in suggestions


def test_real_registry_returns_meaningful_suggestions() -> None:
    """Tier 2: against the live REGISTRY (no override), helper returns
    real command names + ``help``. Smoke test that the default code
    path doesn't crash and produces a sensible list.
    """
    suggestions = suggest_for_unknown("lis")
    # ``list`` should be a close fuzzy match for ``lis``.
    assert "list" in suggestions
    assert "help" in suggestions
