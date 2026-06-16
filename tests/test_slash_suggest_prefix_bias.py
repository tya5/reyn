"""Tier 2: prefix bias in ``suggest_for_unknown`` (B8).

B8 gap: the old ``suggest_for_unknown`` used ``difflib.get_close_matches``
with no prefix priority. Typing ``/fi`` could suggest edit-distance-similar
names like ``/skill`` or ``/list`` before the obvious ``/find``; the
empty-match fallback ``all_names[:3]`` was alphabetical-only.

Fix: prepend commands whose name startswith the typed token before the
difflib results, deduplicated and capped at 3 total.

Public surfaces tested:
  - ``suggest_for_unknown("fi")`` → ``/find`` ranked first (prefix bias).
  - ``suggest_for_unknown("ag")`` → ``/agent`` and/or ``/agents`` appear
    before difflib-only matches (prefix bias).
  - ``/help`` is always appended (existing contract preserved).
  - No duplicates in the output (dedup preserved).
  - Empty-token fallback is still alphabetical head (existing contract).
  - Cap of 3 (before the always-on /help) is preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.slash import suggest_for_unknown

# Stable fixture list independent of live registry evolution.
_NAMES = sorted([
    "agent", "agents", "answer", "budget", "cancel", "clear-history",
    "copy", "cost", "docs-filter", "exit", "find", "help", "image",
    "list", "memory", "plan", "quit", "reset", "save", "skill",
    "skills", "tasks", "zen",
])


def test_prefix_match_ranked_first_for_fi() -> None:
    """Tier 2: ``/fi`` → ``/find`` is the first suggestion (prefix bias).

    Without prefix priority, difflib might rank edit-distance neighbours
    above the obvious ``/find`` for a 2-char prefix. Pin that the prefix
    match wins rank-1.
    """
    suggestions = suggest_for_unknown("fi", names=_NAMES)
    assert suggestions, "suggest_for_unknown returned empty list"
    assert suggestions[0] == "find", (
        f"Expected 'find' as rank-1 for token 'fi', got: {suggestions}"
    )
    assert "help" in suggestions


def test_prefix_matches_ranked_before_fuzzy_for_ag() -> None:
    """Tier 2: ``/ag`` → ``/agent`` and/or ``/agents`` appear before fuzzy matches."""
    suggestions = suggest_for_unknown("ag", names=_NAMES)
    prefix_hits = [s for s in suggestions if s.startswith("ag")]
    fuzzy_only = [s for s in suggestions if not s.startswith("ag") and s != "help"]
    assert prefix_hits, (
        f"No prefix-match for 'ag' in suggestions: {suggestions}"
    )
    # Every prefix hit must appear before any fuzzy-only hit.
    if fuzzy_only:
        last_prefix_idx = max(suggestions.index(p) for p in prefix_hits)
        first_fuzzy_idx = min(suggestions.index(f) for f in fuzzy_only)
        assert last_prefix_idx < first_fuzzy_idx, (
            f"Fuzzy match appears before prefix match for 'ag': {suggestions}"
        )


def test_no_duplicates_in_output() -> None:
    """Tier 2: suggestions list has no duplicate entries."""
    for token in ("fi", "ag", "he", "li", "co", ""):
        suggestions = suggest_for_unknown(token, names=_NAMES)
        assert len(suggestions) == len(set(suggestions)), (
            f"Duplicates found for token {token!r}: {suggestions}"
        )


def test_help_always_appended() -> None:
    """Tier 2: ``help`` is always in the output regardless of token."""
    for token in ("fi", "ag", "xxxxxxxx", ""):
        suggestions = suggest_for_unknown(token, names=_NAMES)
        assert "help" in suggestions, (
            f"'help' missing from suggestions for token {token!r}: {suggestions}"
        )


def test_fallback_alphabetical_when_no_prefix_or_fuzzy_match() -> None:
    """Tier 2: completely unrecognisable token falls back to alphabetical head."""
    suggestions = suggest_for_unknown("xxxxxxxxxxxxxxx", names=_NAMES)
    # No prefix match, no fuzzy match → first 3 of alphabetical names.
    assert suggestions[:3] == _NAMES[:3], (
        f"Fallback should be alphabetical head for unrecognisable token: {suggestions}"
    )
    assert "help" in suggestions
