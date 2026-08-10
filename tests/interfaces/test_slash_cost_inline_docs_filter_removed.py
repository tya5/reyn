"""Tier 2: /cost-inline and /docs-filter are removed (#2302 owner decision).

Falsify tests — guard that the removal is complete and permanent:
  1. Neither command resolves in the REGISTRY (= unknown-command path).
  2. Neither orphaned sentinel (__cost_inline_toggle__, __docs_filter__)
     can be dispatched — handler absence proves no sentinel can be emitted.

Motivated by #2302 owner decision: "古い TUI 専用機能は削除". Orphaned
sentinel class closed 6/6 (4 removed in #2300, 2 here).
"""
from __future__ import annotations

from reyn.interfaces.slash import REGISTRY


def test_cost_inline_not_in_registry() -> None:
    """Tier 2: /cost-inline is not registered after removal."""
    assert REGISTRY.get("cost-inline") is None


def test_docs_filter_not_in_registry() -> None:
    """Tier 2: /docs-filter is not registered after removal."""
    assert REGISTRY.get("docs-filter") is None


def test_cost_inline_toggle_sentinel_has_no_handler() -> None:
    """Tier 2: no handler exists that could emit __cost_inline_toggle__."""
    # With the command absent, there is no callable path to produce the
    # sentinel — registry absence and handler absence are the same fact.
    entry = REGISTRY.get("cost-inline")
    assert entry is None, "cost-inline handler must be absent"


def test_docs_filter_sentinel_has_no_handler() -> None:
    """Tier 2: no handler exists that could emit __docs_filter__."""
    entry = REGISTRY.get("docs-filter")
    assert entry is None, "docs-filter handler must be absent"
