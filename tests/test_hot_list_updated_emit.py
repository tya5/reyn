"""Tier 2: ActionUsageTracker emits hot_list_updated on reorder + forwarder routes (#192).

Pre-fix the hot list ran silently inside ChatSession — TUI had no
signal when ARS routing decisions changed (= which skills/memory
entries are currently "hot"). Per the #192 owner decisions:

  - Q1 (b): emit only when the full sorted **order** changes (not on
    every record() — score-only changes within a stable order do not
    fire).
  - Q2: payload carries the **full** ranking
    ``[{qualified_name, freq, last_ts}, ...]``, not just top-N.
  - Q3: TUI consumes via the existing ChatLifecycleForwarder pattern
    so the Memory tab augmentation can subscribe without polling.

This file pins:
  1. ``ActionUsageTracker`` fires the ``on_ranking_changed`` callback
     when the qualified-name order reorders.
  2. The callback receives the full sorted ranking with freq + last_ts.
  3. Score-only changes within a stable order do NOT re-fire.
  4. ``ChatLifecycleForwarder.on_hot_list_updated`` forwards the
     payload to the outbox as
     ``OutboxMessage(kind="hot_list_updated", meta={"ranking": [...]})``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from reyn.chat.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event
from reyn.tools.action_usage_tracker import ActionUsageTracker

# ── 1. Tracker fires callback on reorder ──────────────────────────────────


def test_tracker_fires_callback_on_first_record() -> None:
    """Tier 2: first record() fires the callback (= ranking goes [] → [name])."""
    seen: list[list[dict]] = []
    tracker = ActionUsageTracker(
        on_ranking_changed=lambda r: seen.append(r),
    )
    tracker.record("file__read")
    assert len(seen) == 1
    assert seen[0][0]["qualified_name"] == "file__read"
    assert seen[0][0]["freq"] == 1
    assert isinstance(seen[0][0]["last_ts"], float)


def test_tracker_fires_callback_on_reorder() -> None:
    """Tier 2: callback fires when the qualified-name order changes."""
    seen: list[list[dict]] = []
    tracker = ActionUsageTracker(
        on_ranking_changed=lambda r: seen.append(r),
    )
    # Establish initial order: A on top.
    tracker.record("file__a")
    tracker.record("file__a")
    tracker.record("file__b")
    initial_calls = len(seen)
    initial_order = [r["qualified_name"] for r in seen[-1]]
    assert initial_order == ["file__a", "file__b"]

    # Promote B by recording it several times — when B's score
    # overtakes A, the order flips and a new callback fires.
    tracker.record("file__b")
    tracker.record("file__b")
    # Should have fired again (= the top-1 changed from A to B).
    assert len(seen) > initial_calls
    new_order = [r["qualified_name"] for r in seen[-1]]
    assert new_order == ["file__b", "file__a"]


def test_tracker_does_not_fire_on_score_only_change() -> None:
    """Tier 2: bumping freq when order is unchanged does NOT re-fire.

    Q1 (b) semantics: the diff granularity is qualified-name order.
    Score-only changes within a stable order (= top item gets recorded
    again) must not produce a re-render signal — that would defeat
    the purpose of deduplication.
    """
    seen: list[list[dict]] = []
    tracker = ActionUsageTracker(
        on_ranking_changed=lambda r: seen.append(r),
    )
    # First record: order = [a]. Fires once.
    tracker.record("a")
    initial = len(seen)
    # Subsequent records of the same name: freq bumps but order stays [a].
    tracker.record("a")
    tracker.record("a")
    tracker.record("a")
    assert len(seen) == initial, (
        "score-only bumps must not re-fire the ranking callback"
    )


def test_tracker_no_callback_when_none_passed() -> None:
    """Tier 2: tracker without callback simply records (= backward-compat)."""
    tracker = ActionUsageTracker()  # no callback
    tracker.record("file__read")  # must not raise
    assert tracker._freq["file__read"] == 1


def test_tracker_swallows_callback_exceptions() -> None:
    """Tier 2: a raising callback must not crash record().

    The tracker is on the hot path of every router turn; an observer
    failure must stay advisory.
    """
    def _boom(_ranking):
        raise RuntimeError("test")

    tracker = ActionUsageTracker(on_ranking_changed=_boom)
    tracker.record("file__a")  # must not raise


def test_full_ranking_includes_freq_and_last_ts() -> None:
    """Tier 2: full_ranking() returns full sorted list with metadata."""
    tracker = ActionUsageTracker()
    tracker.record("a")
    tracker.record("b")
    tracker.record("a")  # a now has freq=2
    ranking = tracker.full_ranking()
    assert len(ranking) == 2
    assert ranking[0]["qualified_name"] == "a"  # freq=2 outranks freq=1
    assert ranking[0]["freq"] == 2
    assert ranking[1]["qualified_name"] == "b"
    assert ranking[1]["freq"] == 1
    for r in ranking:
        assert isinstance(r["last_ts"], float)


# ── 2. ChatLifecycleForwarder routes to outbox ────────────────────────────


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_lifecycle_forwarder_forwards_hot_list_updated() -> None:
    """Tier 2: on_hot_list_updated → OutboxMessage(kind=hot_list_updated)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="hot_list_updated",
        data={"ranking": [
            {"qualified_name": "file__read", "freq": 5, "last_ts": time.time()},
            {"qualified_name": "web__search", "freq": 2, "last_ts": time.time()},
        ]},
    ))
    msgs = _drain(q)
    assert len(msgs) == 1
    assert msgs[0].kind == "hot_list_updated"
    assert msgs[0].text == ""  # data signal, not display
    ranking = msgs[0].meta["ranking"]
    assert len(ranking) == 2
    assert ranking[0]["qualified_name"] == "file__read"
    assert ranking[0]["freq"] == 5


def test_lifecycle_forwarder_empty_ranking_ok() -> None:
    """Tier 2: empty / missing ranking still emits with [] (= signals reset)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="hot_list_updated", data={}))
    msgs = _drain(q)
    assert len(msgs) == 1
    assert msgs[0].meta["ranking"] == []
