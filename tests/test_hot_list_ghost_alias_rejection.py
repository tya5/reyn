"""Tier 2: Ghost alias rejection at hot-list seed load (Part B).

Verifies that ActionUsageTracker._load_from_disk rejects qualified names
that fail structural parse (= unknown category, missing __ separator,
qualified-name corruption) so they never enter the hot list and never
surface as broken aliases to the LLM.

Test cases use:
  - 2 valid entries: file__read, skill__eval (real categories, real names)
  - 1 ghost entry: bogus__nonexistent (unknown category "bogus")

No mocks. Uses real ActionUsageTracker + real split_qualified_name + tmp_path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from reyn.tools.action_usage_tracker import ActionUsageTracker


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Write a list of dicts as JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Ghost rejection: structurally invalid category ────────────────────────────


def test_ghost_alias_rejected_from_hot_list(tmp_path: Path) -> None:
    """Tier 2: a ghost alias with an unknown category is rejected at load.

    The JSONL contains 2 valid entries + 1 ghost. After loading, get_top_n
    must return only the 2 valid entries (plus seed fill), never the ghost.

    Ghost here: bogus__nonexistent — "bogus" is not a known category in
    universal_catalog.CATEGORIES, so split_qualified_name raises ValueError
    and the entry is silently skipped.
    """
    now = time.time()
    persist_path = tmp_path / "action_usage.jsonl"
    _write_jsonl(persist_path, [
        {"qualified_name": "file__read",            "ts": now},
        {"qualified_name": "skill__eval",           "ts": now},
        {"qualified_name": "bogus__nonexistent",    "ts": now},  # ghost
    ])

    tracker = ActionUsageTracker(persist_path=persist_path)
    # Use a large n + no seed so freq-ranked items dominate.
    result = tracker.get_top_n(10, seed=[])

    assert "file__read" in result, "Valid alias file__read must be in hot list."
    assert "skill__eval" in result, "Valid alias skill__eval must be in hot list."
    assert "bogus__nonexistent" not in result, (
        "Ghost alias bogus__nonexistent must be rejected and absent from hot list."
    )


def test_ghost_alias_corrupted_separator_rejected(tmp_path: Path) -> None:
    """Tier 2: a qualified-name with a corrupted separator is rejected.

    B37 W1: default_api.web__search was recorded with a dot-prefix
    corruption so the category portion becomes "default_api" which is
    not in CATEGORIES. The entry must be silently skipped.
    """
    now = time.time()
    persist_path = tmp_path / "action_usage.jsonl"
    _write_jsonl(persist_path, [
        {"qualified_name": "file__list",               "ts": now},
        {"qualified_name": "web__search",              "ts": now},
        {"qualified_name": "default_api.web__search",  "ts": now},  # corrupted
    ])

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(10, seed=[])

    assert "file__list" in result
    assert "web__search" in result
    assert "default_api.web__search" not in result, (
        "Corrupted alias default_api.web__search must be rejected."
    )


def test_valid_aliases_all_loaded(tmp_path: Path) -> None:
    """Tier 2: all structurally valid qualified names are accepted at load.

    Verifies that the ghost-rejection logic does not over-reject valid
    entries from all known resource and operation categories.
    """
    now = time.time()
    persist_path = tmp_path / "action_usage.jsonl"
    valid_entries = [
        "file__read",
        "file__write",
        "file__grep",
        "web__search",
        "web__fetch",
        "rag.operation__drop_source",
        "memory.operation__remember_shared",
        "reyn.source__list",
        "skill__eval",
        "skill__mcp_search",
    ]
    _write_jsonl(persist_path, [
        {"qualified_name": qn, "ts": now} for qn in valid_entries
    ])

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(len(valid_entries) + 5, seed=[])

    for qn in valid_entries:
        assert qn in result, (
            f"Valid alias {qn!r} was incorrectly rejected at load."
        )


def test_ghost_rejected_does_not_enter_seed_fill(tmp_path: Path) -> None:
    """Tier 2: ghost alias rejected at load cannot re-enter via seed fill.

    Even if the ghost name appears in the seed list passed to get_top_n,
    the seed fill path is a separate concern (hot list building, not load).
    This test focuses on the load-time rejection: freq/recency state for
    the ghost must be zero after loading, so it produces no score and
    cannot outrank seed items.

    Verifies indirectly: ghost is absent from freq state → get_top_n
    without seed returns empty for the ghost name.
    """
    now = time.time()
    persist_path = tmp_path / "action_usage.jsonl"
    _write_jsonl(persist_path, [
        {"qualified_name": "bogus__nonexistent", "ts": now},
        {"qualified_name": "bogus__nonexistent", "ts": now},  # 2 records
    ])

    tracker = ActionUsageTracker(persist_path=persist_path)
    # No seed; no valid freq entries → empty result
    result = tracker.get_top_n(5, seed=[])

    assert result == [], (
        "Ghost alias must produce no freq state; result must be empty without seed."
    )
