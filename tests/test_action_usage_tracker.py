"""Tier 2: ActionUsageTracker — compacted freq+recency table.

FP-0034 §D2 / §D16, post-refactor.

Coverage:
  - No records → get_top_n returns only seed items (up to n).
  - n <= 0 → empty list.
  - merge_compacted: freq accumulation + last_ts max + invalid filter.
  - Recency boost (= more recent last_ts ranks higher at equal freq).
  - Seed deduplication + seed order preservation.
  - persist_path=None → memory-only operation, no files created.
  - persist_path set → merge_compacted writes JSON table; reload restores state.
  - Corrupt JSON → empty state fallback (non-fatal).
  - live_records combined with compacted table.
  - on_ranking_changed callback fires only on order change.

No mocks (CLAUDE.md testing policy). Uses real instances + tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.tools.action_usage_tracker import (
    DEFAULT_HOT_LIST_SEED,
    ActionUsageTracker,
    _is_valid_qualified_name,
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
    tracker.merge_compacted([("skill__foo", 1000.0)])
    assert tracker.get_top_n(0, seed=["file__read"]) == []


def test_n_negative_returns_empty() -> None:
    """Tier 2: n<0 returns empty list."""
    tracker = ActionUsageTracker(persist_path=None)
    assert tracker.get_top_n(-1, seed=["file__read"]) == []


# ── 3. merge_compacted: freq ranking ─────────────────────────────────────────


def test_higher_freq_ranks_first() -> None:
    """Tier 2: action merged more times appears before less-merged action."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([
        ("skill__rare", 1000.0),
        ("skill__popular", 1000.0),
        ("skill__popular", 1001.0),
        ("skill__popular", 1002.0),
        ("skill__popular", 1003.0),
        ("skill__popular", 1004.0),
    ])

    result = tracker.get_top_n(2, seed=[])
    assert result[0] == "skill__popular"
    assert result[1] == "skill__rare"


def test_freq_ranking_n_clips_result() -> None:
    """Tier 2: result is clipped to n even when more items are recorded."""
    tracker = ActionUsageTracker(persist_path=None)
    for name in ["skill__a", "skill__b", "skill__c"]:
        tracker.merge_compacted([(name, 1000.0)] * 3)

    result = tracker.get_top_n(2, seed=[])
    # All three have freq=3, so order is alphabetical (= determinism tie-break).
    # n=2 clips to the first two.
    assert result == ["skill__a", "skill__b"]


# ── 4. Recency boost ─────────────────────────────────────────────────────────


def test_recency_breaks_freq_ties(tmp_path: Path) -> None:
    """Tier 2: at equal freq, the more recent last_ts ranks first."""
    tracker = ActionUsageTracker(persist_path=None)
    # both reach freq=2, but skill__recent's last_ts is later
    tracker.merge_compacted([
        ("skill__old", 1000.0),
        ("skill__old", 1001.0),
        ("skill__recent", 1000.0),
        ("skill__recent", 9_000_000.0),  # far future
    ])
    result = tracker.get_top_n(2, seed=[])
    assert result == ["skill__recent", "skill__old"]


# ── 5. Invalid-name filter ───────────────────────────────────────────────────


def test_merge_drops_invalid_qualified_names() -> None:
    """Tier 2: wrapper invocations (no `__` separator) and stale rename
    artifacts are dropped at merge time — they never reach the compacted
    table.
    """
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([
        # valid
        ("file__read", 1000.0),
        # invalid: wrapper name (no __ separator)
        ("list_actions", 1001.0),
        ("describe_action", 1002.0),
        # invalid: stale rename artifact (bare name)
        ("read", 1003.0),
        # invalid: unknown category prefix
        ("bogus__nonexistent", 1004.0),
    ])
    ranking = tracker.full_ranking()
    qns = {r["qualified_name"] for r in ranking}
    assert qns == {"file__read"}


def test_is_valid_qualified_name_recognises_wrappers_as_invalid() -> None:
    """Tier 2: ``_is_valid_qualified_name`` returns False for the
    universal-catalog wrapper names. Documents the contract used by
    both merge and live-scan paths.
    """
    assert not _is_valid_qualified_name("list_actions")
    assert not _is_valid_qualified_name("describe_action")
    assert not _is_valid_qualified_name("search_actions")
    assert not _is_valid_qualified_name("invoke_action")
    assert not _is_valid_qualified_name("read")  # stale rename
    assert not _is_valid_qualified_name("")
    # Sanity: a legitimate qualified name passes.
    assert _is_valid_qualified_name("file__read")
    assert _is_valid_qualified_name("skill__code_review")


# ── 6. Seed deduplication + order ────────────────────────────────────────────


def test_seed_deduped_against_ranked() -> None:
    """Tier 2: seed items already in ranked output are not duplicated."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([("file__read", 1000.0), ("file__read", 1001.0)])

    seed = ["file__read", "web__search"]
    result = tracker.get_top_n(3, seed=seed)

    assert result.count("file__read") == 1
    assert "web__search" in result


def test_seed_fills_remaining_slots_in_order() -> None:
    """Tier 2: seed items fill slots in their declared order after ranked items."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([("skill__x", 1000.0)])

    seed = ["file__read", "web__search", "file__grep"]
    result = tracker.get_top_n(4, seed=seed)

    assert result[0] == "skill__x"
    # seed items must appear in declared order
    seed_portion = [r for r in result if r in seed]
    assert seed_portion == seed


# ── 7. persist_path=None — memory-only ───────────────────────────────────────


def test_memory_only_no_file_created(tmp_path: Path) -> None:
    """Tier 2: persist_path=None never creates any file."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([("skill__foo", 1000.0), ("skill__bar", 1001.0)])
    # directory should have no files
    assert list(tmp_path.iterdir()) == []


# ── 8. JSON persistence ──────────────────────────────────────────────────────


def test_merge_compacted_writes_json_table(tmp_path: Path) -> None:
    """Tier 2: merge_compacted() writes a JSON object keyed by
    qualified_name, with ``{count, last_ts}`` values.
    """
    persist_path = tmp_path / "action_usage.json"
    tracker = ActionUsageTracker(persist_path=persist_path)
    tracker.merge_compacted([
        ("file__read", 1000.0),
        ("file__read", 1500.0),
        ("skill__foo", 2000.0),
    ])

    assert persist_path.exists()
    obj = json.loads(persist_path.read_text(encoding="utf-8"))
    assert obj == {
        "file__read": {"count": 2, "last_ts": 1500.0},
        "skill__foo": {"count": 1, "last_ts": 2000.0},
    }


def test_reload_restores_compacted_state(tmp_path: Path) -> None:
    """Tier 2: a new tracker instance loaded from JSON restores the
    compacted table — ranking matches the pre-reload state.
    """
    persist_path = tmp_path / "action_usage.json"

    tracker1 = ActionUsageTracker(persist_path=persist_path)
    tracker1.merge_compacted([
        ("skill__popular", 1000.0),
        ("skill__popular", 1001.0),
        ("skill__popular", 1002.0),
        ("skill__rare", 1003.0),
    ])

    tracker2 = ActionUsageTracker(persist_path=persist_path)
    result = tracker2.get_top_n(2, seed=[])
    assert result == ["skill__popular", "skill__rare"]


def test_reload_then_merge_accumulates(tmp_path: Path) -> None:
    """Tier 2: reloaded tracker accumulates new merge calls on top of
    the disk-loaded counts.
    """
    persist_path = tmp_path / "action_usage.json"

    tracker1 = ActionUsageTracker(persist_path=persist_path)
    tracker1.merge_compacted([("skill__a", 1000.0), ("skill__a", 1001.0)])
    tracker1.merge_compacted([("skill__b", 1002.0)])

    tracker2 = ActionUsageTracker(persist_path=persist_path)
    tracker2.merge_compacted([("skill__b", 1003.0)])  # b now total=2

    ranking = {
        r["qualified_name"]: r["freq"] for r in tracker2.full_ranking()
    }
    assert ranking == {"skill__a": 2, "skill__b": 2}


# ── 9. Corrupt JSON fallback ─────────────────────────────────────────────────


def test_corrupt_json_falls_back_to_empty_state(tmp_path: Path) -> None:
    """Tier 2: a corrupt JSON produces empty state, not a crash."""
    persist_path = tmp_path / "action_usage.json"
    persist_path.write_text("not valid json\n", encoding="utf-8")

    tracker = ActionUsageTracker(persist_path=persist_path)
    result = tracker.get_top_n(3, seed=["seed_item"])
    assert result == ["seed_item"]


def test_partially_invalid_json_skips_bad_entries(tmp_path: Path) -> None:
    """Tier 2: partial corruption — entries with bad shape are skipped,
    valid entries are loaded.
    """
    persist_path = tmp_path / "action_usage.json"
    persist_path.write_text(json.dumps({
        "file__read": {"count": 3, "last_ts": 1000.0},
        # missing count
        "skill__bad1": {"last_ts": 1000.0},
        # non-numeric count
        "skill__bad2": {"count": "three", "last_ts": 1000.0},
        # invalid qualified_name
        "list_actions": {"count": 5, "last_ts": 1000.0},
    }), encoding="utf-8")

    tracker = ActionUsageTracker(persist_path=persist_path)
    ranking = {
        r["qualified_name"]: r["freq"] for r in tracker.full_ranking()
    }
    assert ranking == {"file__read": 3}


# ── 10. live_records combine with compacted ──────────────────────────────────


def test_live_records_combined_with_compacted() -> None:
    """Tier 2: live_records passed to full_ranking / get_top_n are
    merged with the compacted table for ranking.
    """
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([("skill__a", 1000.0), ("skill__a", 1001.0)])

    # skill__b appears only in live_records but has freq=3 there.
    live = [
        ("skill__b", 5000.0),
        ("skill__b", 5001.0),
        ("skill__b", 5002.0),
    ]
    result = tracker.get_top_n(2, seed=[], live_records=live)
    # b: freq=3 + recent last_ts → ranks first; a: freq=2 → second.
    assert result == ["skill__b", "skill__a"]


def test_live_records_only_no_compacted() -> None:
    """Tier 2: a tracker with empty compacted table still ranks live
    records when supplied; seed fills any remaining slots.
    """
    tracker = ActionUsageTracker(persist_path=None)
    live = [
        ("skill__x", 1000.0),
        ("skill__x", 1001.0),
        ("skill__y", 1002.0),
    ]
    result = tracker.get_top_n(3, seed=["file__read"], live_records=live)
    assert result[0] == "skill__x"
    assert result[1] == "skill__y"
    assert result[2] == "file__read"


def test_live_records_filter_invalid_names() -> None:
    """Tier 2: live_records pass through the same _is_valid_qualified_name
    filter so wrapper-name invocations on the wire never leak into the
    ranking even if upstream extraction missed them.
    """
    tracker = ActionUsageTracker(persist_path=None)
    live = [
        ("file__read", 1000.0),
        ("list_actions", 1001.0),  # wrapper — must be dropped
        ("read", 1002.0),          # stale rename — must be dropped
    ]
    ranking = tracker.full_ranking(live_records=live)
    qns = {r["qualified_name"] for r in ranking}
    assert qns == {"file__read"}


# ── 11. on_ranking_changed callback ──────────────────────────────────────────


def test_on_ranking_changed_fires_on_order_change(tmp_path: Path) -> None:
    """Tier 2: the callback fires when the qualified-name ORDER of the
    ranking changes after a merge_compacted call.
    """
    observed: list[list[str]] = []
    tracker = ActionUsageTracker(
        persist_path=None,
        on_ranking_changed=lambda ranking: observed.append(
            [r["qualified_name"] for r in ranking]
        ),
    )

    # First merge introduces skill__a → ranking order becomes [a].
    tracker.merge_compacted([("skill__a", 1000.0)])
    # Second merge introduces skill__b above a → order changes to [b, a].
    tracker.merge_compacted([("skill__b", 2000.0), ("skill__b", 2001.0)])

    assert observed == [["skill__a"], ["skill__b", "skill__a"]]


def test_on_ranking_changed_silent_when_order_stable() -> None:
    """Tier 2: the callback does NOT fire when only counts/recency
    change but the qualified-name order is stable.
    """
    observed: list[list[str]] = []
    tracker = ActionUsageTracker(
        persist_path=None,
        on_ranking_changed=lambda ranking: observed.append(
            [r["qualified_name"] for r in ranking]
        ),
    )

    tracker.merge_compacted([("skill__a", 1000.0)])
    initial = list(observed)
    # Bump a's count — order [a] stays [a]; no new callback fire.
    tracker.merge_compacted([("skill__a", 1001.0)])
    assert observed == initial


# ── 12. Seed contract preserved (default) ────────────────────────────────────


def test_default_seed_items_are_qualified_names() -> None:
    """Tier 2: every DEFAULT_HOT_LIST_SEED entry is a structurally valid
    qualified name (= passes the same filter as merge / live-scan).
    """
    for name in DEFAULT_HOT_LIST_SEED:
        assert _is_valid_qualified_name(name), (
            f"DEFAULT_HOT_LIST_SEED entry {name!r} fails the qualified-name "
            f"filter — would be dropped if it ever reached the tracker via "
            f"merge or live scan."
        )


def test_default_seed_static_entries_have_routing_rules() -> None:
    """Tier 2: every DEFAULT_HOT_LIST_SEED entry under a static operation
    category (= file, web, memory_operation, reyn_source, rag_operation,
    mcp.operation, exec) has a corresponding routing rule. Without this
    pin a missing rule would surface UnknownActionError to the LLM as
    soon as the seeded alias is invoked.
    """
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    static_prefixes = (
        "file__",
        "web__",
        "memory_operation__",
        "reyn_source__",
        "rag_operation__",
        "mcp.operation__",
        "exec__",
    )
    for name in DEFAULT_HOT_LIST_SEED:
        if name.startswith(static_prefixes):
            assert name in _OPERATION_RULES, (
                f"DEFAULT_HOT_LIST_SEED entry {name!r} has no routing rule "
                f"in _OPERATION_RULES."
            )
