"""Tier 1: Contract — plugin source ``kind`` precedence on name collision (ADR 0064 §3.8, #3067).

Pins: ``builtin`` wins a same-name collision over ``local`` and ``git``;
``local`` wins over ``git``; order of the input list does not matter
(the precedence is a property of the kinds, not of arrival order).
"""
from __future__ import annotations

import pytest

from reyn.plugins.source import PLUGIN_SOURCE_PRECEDENCE, resolve_name_collision


def test_builtin_wins_over_local_and_git():
    """Tier 1: ``builtin`` wins a 3-way collision (ADR §3.8: builtin priority)."""
    assert resolve_name_collision(["local", "git", "builtin"]) == "builtin"


def test_local_wins_over_git():
    """Tier 1: ``local`` wins over ``git`` absent a ``builtin`` candidate."""
    assert resolve_name_collision(["git", "local"]) == "local"


def test_order_independent():
    """Tier 1: the winner does not depend on the input list's order."""
    assert resolve_name_collision(["git", "builtin", "local"]) == "builtin"
    assert resolve_name_collision(["builtin", "local", "git"]) == "builtin"


def test_single_candidate_returns_itself():
    """Tier 1: a single candidate (no actual collision) returns itself for
    every kind in the precedence order."""
    for kind in PLUGIN_SOURCE_PRECEDENCE:
        assert resolve_name_collision([kind]) == kind


def test_empty_candidates_raises():
    """Tier 1: an empty candidate list is a caller error, not a silent
    default winner."""
    with pytest.raises(ValueError):
        resolve_name_collision([])
