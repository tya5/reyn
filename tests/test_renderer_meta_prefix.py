"""Tier 2: renderer _meta_prefix — provenance-prefix helper contracts.

`_meta_prefix` builds a `[skill_name#run_id_short] ` prefix from outbox message
meta.  It has four branches (both present / skill only / short only / neither) all
used by `format_inline_message`.  Pinning them independently of the full renderer
prevents a silent branch-removal from collapsing provenance labels.
"""
from __future__ import annotations

from reyn.interfaces.repl.renderer import _meta_prefix


def test_meta_prefix_both_skill_and_short() -> None:
    """Tier 2: both skill_name and run_id_short → '[skill#short] '."""
    assert _meta_prefix({"skill_name": "builder", "run_id_short": "ab12"}) == "[builder#ab12] "


def test_meta_prefix_skill_only() -> None:
    """Tier 2: skill_name only (no run_id_short) → '[skill] '."""
    assert _meta_prefix({"skill_name": "planner"}) == "[planner] "


def test_meta_prefix_short_only() -> None:
    """Tier 2: run_id_short only (no skill_name) → '[#short] '."""
    assert _meta_prefix({"run_id_short": "cd34"}) == "[#cd34] "


def test_meta_prefix_neither_returns_empty() -> None:
    """Tier 2: no skill or short → '' (clean messages have no prefix)."""
    assert _meta_prefix({}) == ""
    assert _meta_prefix({"other_key": "x"}) == ""
