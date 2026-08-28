"""Tier 1: #5130 — ``_ResolvedAgent`` cannot be constructed from a bare
string outside its two resolver functions.

lead-coder's own #5432 review: the PR body recorded a manual mypy run and a
manual ``python3 -c`` runtime check showing ``_ResolvedAgent("x")`` fails —
but neither of those runs again after merge. mypy's own half is covered
every CI run (`mypy_ratchet.py`), but nothing pinned the RUNTIME half: a
future edit that adds ``_token: _AgentResolutionToken = None`` (a default)
would make ``_ResolvedAgent("x")`` construct silently, and this defense
would be gone with no test to notice.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.endpoint import (
    _AgentResolutionToken,
    _ResolvedAgent,
)


def test_bare_construction_without_a_token_raises():
    """Tier 1: the exact call shape #5130's own review named —
    ``_ResolvedAgent("x")`` with no token argument at all — must raise at
    construction time, not merely be flagged by mypy (which runs at
    lint-time only, and is separately baselined by mypy_ratchet.py).

    Strip-falsifier: giving ``_token`` a default value of ``None`` in
    ``_ResolvedAgent``'s own definition turns this green-by-vacuity into
    an actual pass with no token — this test would then need to also
    check the value, not just the raise, to catch that regression; see
    the sibling test below for that half."""
    with pytest.raises(TypeError):
        _ResolvedAgent("bare-string")  # type: ignore[call-arg]


def test_construction_with_a_real_token_succeeds():
    """Tier 1: non-vacuity for the test above — a genuine
    ``_AgentResolutionToken`` (the only thing either resolver function
    ever passes) must still construct successfully. Without this, the
    sibling test's ``pytest.raises(TypeError)`` could pass for the wrong
    reason (e.g. a typo'd constructor that always raises)."""
    real_token = _AgentResolutionToken()
    resolved = _ResolvedAgent("legit-name", real_token)
    assert resolved.name == "legit-name"
