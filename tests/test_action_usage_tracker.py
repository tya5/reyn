"""Tier 2: ActionUsageTracker — freq+recency scoring and seed semantics.

FP-0034 §D2 / §D16.

Coverage:
  - No records → get_top_n returns only seed items (up to n).
  - Multiple records → freq-ranked items dominate, higher freq wins.
  - Seed deduplication against freq-ranked items.
  - persist_path=None → memory-only operation, no files created.
  - persist_path set → events written to JSONL; reloaded on new instance.
  - Corrupt JSONL → empty state fallback (non-fatal).
  - n <= 0 → empty list.
  - Seed order preserved within seed slots.

No mocks (CLAUDE.md testing policy). Uses real instances + tmp_path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from reyn.tools.action_usage_tracker import (
    DEFAULT_HOT_LIST_SEED,
    ActionUsageTracker,
)

# ── 1. No records — seed fills result ────────────────────────────────────────


def test_no_records_returns_seed() -> None:
    """Tier 2: get_top_n with no records returns seed items up to n."""
    tracker = ActionUsageTracker(persist_path=None)
    seed = ["file__read", "web__search", "file__grep"]
    result = tracker.get_top_n(2, seed=seed)
    assert result == ["file__read", "web__search"]


def test_no_records_n_larger_than_seed() -> None:
    """Tier 2: n larger than seed returns all seed items (no padding beyond)."""
    tracker = ActionUsageTracker(persist_path=None)
    seed = ["file__read", "web__search"]
    result = tracker.get_top_n(5, seed=seed)
    assert result == ["file__read", "web__search"]


def test_no_records_empty_seed() -> None:
    """Tier 2: no records + empty seed → empty result."""
    tracker = ActionUsageTracker(persist_path=None)
    result = tracker.get_top_n(5, seed=[])
    assert result == []


# ── 2. n <= 0 edge case ───────────────────────────────────────────────────────


def test_n_zero_returns_empty() -> None:
    """Tier 2: n=0 always returns empty list."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.record("skill__foo")
    assert tracker.get_top_n(0, seed=["file__read"]) == []


def test_n_negative_returns_empty() -> None:
    """Tier 2: n<0 returns empty list."""
    tracker = ActionUsageTracker(persist_path=None)
    assert tracker.get_top_n(-1, seed=["file__read"]) == []


# ── 3. Freq ranking ───────────────────────────────────────────────────────────


def test_higher_freq_ranks_first() -> None:
    """Tier 2: action recorded more times appears before less-recorded action."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.record("skill__rare")
    for _ in range(5):
        tracker.record("skill__popular")

    result = tracker.get_top_n(2, seed=[])
    assert result[0] == "skill__popular"
    assert result[1] == "skill__rare"


def test_freq_ranking_n_clips_result() -> None:
    """Tier 2: result is clipped to n even when more items are recorded."""
    tracker = ActionUsageTracker(persist_path=None)
    for name in ["skill__a", "skill__b", "skill__c"]:
        for _ in range(3):
            tracker.record(name)

    result = tracker.get_top_n(2, seed=[])
    assert len(result) == 2


# ── 4. Seed deduplication ─────────────────────────────────────────────────────


def test_seed_deduped_against_freq_ranked() -> None:
    """Tier 2: seed items already in freq-ranked output are not duplicated."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.record("file__read")
    tracker.record("file__read")

    # seed includes file__read which is already freq-ranked
    seed = ["file__read", "web__search"]
    result = tracker.get_top_n(3, seed=seed)

    assert result.count("file__read") == 1
    assert "web__search" in result


def test_seed_fills_remaining_slots_in_order() -> None:
    """Tier 2: seed items fill slots in their original order after freq items."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.record("skill__x")

    seed = ["seed_a", "seed_b", "seed_c"]
    result = tracker.get_top_n(4, seed=seed)

    assert result[0] == "skill__x"
    # seed items must appear in original order
    seed_portion = [r for r in result if r in seed]
    assert seed_portion == ["seed_a", "seed_b", "seed_c"]


# ── 5. persist_path=None — memory-only ───────────────────────────────────────


def test_memory_only_no_file_created(tmp_path: Path) -> None:
    """Tier 2: persist_path=None never creates any file."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.record("skill__foo")
    tracker.record("skill__bar")
    # directory should have no jsonl file
    assert not any(tmp_path.iterdir())


# ── 6. JSONL persistence ──────────────────────────────────────────────────────


def test_events_written_to_jsonl(tmp_path: Path) -> None:
    """Tier 2: record() appends a valid JSONL line to persist_path."""
    persist_path = tmp_path / "action_usage.jsonl"
    tracker = ActionUsageTracker(persist_path=persist_path)
    tracker.record("skill__foo")
    tracker.record("file__read")

    assert persist_path.exists()
    lines = persist_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        entry = json.loads(line)
        assert "qualified_name" in entry
        assert "ts" in entry
        assert isinstance(entry["ts"], float)


def test_reload_restores_freq_state(tmp_path: Path) -> None:
    """Tier 2: a new tracker instance loaded from JSONL restores freq ranking."""
    persist_path = tmp_path / "action_usage.jsonl"

    # First instance: record events
    tracker1 = ActionUsageTracker(persist_path=persist_path)
    for _ in range(3):
        tracker1.record("skill__popular")
    tracker1.record("skill__rare")

    # Second instance: load from disk
    tracker2 = ActionUsageTracker(persist_path=persist_path)
    result = tracker2.get_top_n(2, seed=[])
    assert result[0] == "skill__popular"
    assert result[1] == "skill__rare"


def test_reload_after_additional_records(tmp_path: Path) -> None:
    """Tier 2: reloaded tracker merges disk state with new in-session records."""
    persist_path = tmp_path / "action_usage.jsonl"

    tracker1 = ActionUsageTracker(persist_path=persist_path)
    tracker1.record("skill__a")
    tracker1.record("skill__a")
    tracker1.record("skill__b")

    tracker2 = ActionUsageTracker(persist_path=persist_path)
    tracker2.record("skill__b")  # now b has 2 total (1 on disk + 1 new)

    result = tracker2.get_top_n(2, seed=[])
    # both a and b have freq=2, result must contain both
    assert set(result) == {"skill__a", "skill__b"}


# ── 7. Corrupt JSONL fallback ─────────────────────────────────────────────────


def test_corrupt_jsonl_falls_back_to_empty_state(tmp_path: Path) -> None:
    """Tier 2: a corrupt JSONL produces empty state, not a crash."""
    persist_path = tmp_path / "action_usage.jsonl"
    persist_path.write_text("not valid json\n{broken\n", encoding="utf-8")

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(3, seed=["seed_item"])
    # freq state is empty; seed fills the slot
    assert result == ["seed_item"]


def test_partially_corrupt_jsonl_skips_bad_lines(tmp_path: Path) -> None:
    """Tier 2: partial corruption — valid lines are parsed; invalid lines skipped."""
    persist_path = tmp_path / "action_usage.jsonl"
    good_line = json.dumps({"qualified_name": "skill__good", "ts": time.time()})
    persist_path.write_text(f"{good_line}\nnot-json\n", encoding="utf-8")

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(1, seed=[])
    assert result == ["skill__good"]


def test_missing_fields_in_jsonl_skipped(tmp_path: Path) -> None:
    """Tier 2: JSONL lines missing required fields are skipped silently."""
    persist_path = tmp_path / "action_usage.jsonl"
    # missing ts
    bad1 = json.dumps({"qualified_name": "skill__nots"})
    # missing qualified_name
    bad2 = json.dumps({"ts": 1716000000.0})
    # valid
    good = json.dumps({"qualified_name": "skill__ok", "ts": 1716000000.0})
    persist_path.write_text(f"{bad1}\n{bad2}\n{good}\n", encoding="utf-8")

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(3, seed=[])
    assert result == ["skill__ok"]


# ── 8. DEFAULT_HOT_LIST_SEED ──────────────────────────────────────────────────


def test_default_seed_has_sixteen_items() -> None:
    """Tier 2: DEFAULT_HOT_LIST_SEED contains exactly 16 entries.

    Seed growth log:
      - file__grep removed (B27-M2): §D20 file-ops not yet implemented
        as ToolDefinitions.
      - file__list + reyn.source__list added (B27 S6 follow-up): cold-
        start directory-listing intent gets a real action instead of
        misusing list_actions as a filesystem finder.
      - skill__index_docs added (B28-MED-1): RAG indexing intent
        surfaces a real skill rather than hallucinating
        rag.operation__add_source.
      - skill__eval added (B30-NEW-2): eval intent has a discoverable
        cold-start path. Companion to the hot_list_n bump in
        ActionRetrievalConfig — both together close the
        discoverability-vs-disambiguation gap surfaced by dogfood B30
        worker 2 (= new hallucination variant skill__direct_llm_eval
        when skill__eval was missing from the seed).
      - file__grep + file__glob re-added (B34): W3 ablation confirmed
        LLM picks file__list for grep/glob intent (KeyError:'path') when
        file__grep is absent from the hot list. ToolDefinitions now
        implemented; routing rules added; seed re-seeded (B27-M2 closed).
      - file__write + rag.operation__drop_source added (B37 W4/W6):
        D2-wrapper scope is hot-list-only; seeding these ensures the LLM
        sees canonical arg schemas (write: content; drop_source: source)
        from the first turn instead of hallucinating wrong key names.
      - skill__read_local_files removed (B46 cleanup, 2026-05-21): the
        skill duplicated the router's inline file__read path with no
        added value; removed from stdlib alongside its seed entry.
    """
    assert len(DEFAULT_HOT_LIST_SEED) == 16


def test_default_seed_fits_within_default_hot_list_n() -> None:
    """Tier 2: ActionRetrievalConfig.hot_list_n default must cover the seed.

    B30 surfaced a silent-truncation bug: DEFAULT_HOT_LIST_SEED grew
    to 12 entries while ActionRetrievalConfig.hot_list_n stayed at 10,
    so the last 2 seed entries (skill__read_local_files,
    skill__index_docs) were silently dropped from the cold-start hot
    list. The fix (B30-NEW-1) bumped hot_list_n default to 16. This
    invariant prevents the gap from reopening as the seed grows.
    """
    from reyn.config import ActionRetrievalConfig

    default_cfg = ActionRetrievalConfig()
    assert default_cfg.hot_list_n >= len(DEFAULT_HOT_LIST_SEED), (
        f"ActionRetrievalConfig.hot_list_n default ({default_cfg.hot_list_n}) "
        f"is smaller than DEFAULT_HOT_LIST_SEED length ({len(DEFAULT_HOT_LIST_SEED)}). "
        f"Cold-start hot list will silently truncate seed entries. "
        f"Bump hot_list_n in src/reyn/config.py or shrink the seed."
    )


def test_default_seed_items_are_strings() -> None:
    """Tier 2: all DEFAULT_HOT_LIST_SEED entries are non-empty strings."""
    for item in DEFAULT_HOT_LIST_SEED:
        assert isinstance(item, str) and item


def test_hot_list_seed_static_entries_have_routing_rules() -> None:
    """Tier 2: every static-category name in DEFAULT_HOT_LIST_SEED is
    routable via _OPERATION_RULES (= consistency invariant). A static
    name in the seed without a routing rule would surface
    UnknownActionError to the LLM as soon as the alias is invoked.
    """
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    static_prefixes = (
        "file__",
        "web__",
        "memory.operation__",
        "reyn.source__",
        "rag.operation__",
        "mcp.operation__",
        "exec__",
    )
    for name in DEFAULT_HOT_LIST_SEED:
        if name.startswith(static_prefixes):
            assert name in _OPERATION_RULES, (
                f"DEFAULT_HOT_LIST_SEED entry {name!r} has no routing rule "
                f"in _OPERATION_RULES. Either add the rule or remove from "
                f"the seed."
            )
