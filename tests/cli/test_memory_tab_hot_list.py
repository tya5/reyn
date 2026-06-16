"""Tier 2: Memory tab renders a "Hot now" sub-section when hot_list is non-empty.

Issue #192 — ``ChatLifecycleForwarder.on_hot_list_updated`` (PR #211)
emits ``OutboxMessage(kind="hot_list_updated", text="",
meta={"ranking": [{qualified_name, freq, last_ts}, ...]})`` whenever
``ActionUsageTracker.record()`` detects a qualified-name order change.
The Memory tab surfaces the latest ranking as a "Hot now" sub-section
above SHARED / AGENT scopes.

Contract pinned here:

1. Empty / missing ``hot_list`` → "HOT NOW" header still renders with
   a dim ``(no router activity yet)`` placeholder underneath (wave-4
   PC5: the section was changed from "omitted entirely when empty" to
   "always-visible header" so the feature is discoverable on cold
   start).
2. Non-empty ``hot_list`` → "HOT NOW" header appears + each entry's
   qualified_name + freq lands in the output.
3. ``flat_entries`` is NOT polluted with hot-list rows (they are
   action qualified-names, not MemoryEntry items — they shouldn't
   participate in j/k cursor navigation over memory).
4. Malformed entries (missing keys / wrong types) are skipped, not
   crashed-on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _project_with_empty_memory(tmp_path: Path) -> Path:
    """Build a minimum project root the renderer can walk."""
    (tmp_path / ".reyn" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_no_hot_list_shows_placeholder(tmp_path):
    """Tier 2: empty hot_list still renders the HOT NOW header
    with a ``(no router activity yet)`` placeholder.

    Wave-4 PC5: the cold-start behavior was changed from "section
    omitted entirely" to "always-visible section with placeholder"
    so the feature is discoverable on first launch / before any
    router activity. Previously the section only appeared after
    ARS emitted ``hot_list_updated`` for the first time, leaving
    new users with no idea the feature existed.
    """
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=None,
    )
    assert "HOT NOW" in rendered
    assert "no router activity yet" in rendered

    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=[],
    )
    assert "HOT NOW" in rendered
    assert "no router activity yet" in rendered


def test_hot_list_renders_qualified_name_and_freq(tmp_path):
    """Tier 2: each ranking entry's name + freq surface in the rendered output."""
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [
        {"qualified_name": "skill__direct_llm", "freq": 7, "last_ts": "..."},
        {"qualified_name": "skill__eval", "freq": 3, "last_ts": "..."},
    ]
    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    assert "HOT NOW" in rendered
    assert "skill__direct_llm" in rendered
    assert "skill__eval" in rendered
    # The frequency markers should be present (the exact formatting is
    # not pinned — only that the count is visible somewhere).
    assert "×7" in rendered
    assert "×3" in rendered


def test_hot_list_does_not_populate_flat_entries(tmp_path):
    """Tier 2: hot-list rows are not memory entries → not in flat_entries.

    ``flat_entries`` drives j/k cursor navigation over MemoryEntry items.
    Hot-list rows are action qualified-names (not MemoryEntry), so they
    must NOT participate in that cursor or the Enter→preview integration
    will receive a dict where it expects a MemoryEntry.
    """
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [{"qualified_name": "skill__direct_llm", "freq": 1, "last_ts": ""}]
    _rendered, flat_entries, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    # Empty project memory + 1 hot row → flat_entries stays empty.
    assert flat_entries == []


def test_hot_list_skips_malformed_entries_without_crashing(tmp_path):
    """Tier 2: missing-keys / wrong-types entries don't crash the render.

    The forwarder normalises shape but a partial-roll out (= older OS
    image emitting a thin payload) shouldn't take down the Memory tab.
    """
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [
        {"qualified_name": "skill__direct_llm", "freq": 5, "last_ts": ""},
        {"qualified_name": "", "freq": 1},          # empty name → skipped
        {"freq": 1},                                 # missing name → skipped
        {"qualified_name": "skill__eval", "freq": "not-an-int"},  # bad type → skipped
        "not-a-dict",                                # bad shape → skipped
    ]
    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    assert "HOT NOW" in rendered
    assert "skill__direct_llm" in rendered
    # Malformed entries are skipped (they should NOT appear in output).
    # The only successfully-rendered entry stays visible.
    assert "skill__eval" not in rendered


def test_hot_list_overflow_marker(tmp_path):
    """Tier 2: ranking longer than the visible cap shows ``… N more``."""
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [
        {"qualified_name": f"skill__entry_{i}", "freq": 10 - i, "last_ts": ""}
        for i in range(12)
    ]
    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    # 12 entries; cap is 8 in the renderer → 4 should be hidden.
    assert "more" in rendered


def test_hot_list_renders_relative_time_when_last_ts_valid(tmp_path):
    """Tier 2: a valid ``last_ts`` float surfaces a relative-time hint.

    The ARS forwarder emits ``last_ts`` as a Unix-epoch float (see
    ``ActionUsageTracker.full_ranking``). Without this hint the user
    has no way to tell whether the ranking is fresh or stale — same
    ``×N`` count can mean "this turn" or "yesterday".
    """
    import time as _time

    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [
        {
            "qualified_name": "skill__direct_llm",
            "freq": 5,
            "last_ts": _time.time() - 90,  # 90 seconds ago → "1m ago"
        }
    ]
    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    # The exact suffix wording is not pinned — only that *some* relative
    # marker ("ago" substring) appears. Tolerates 89s / 90s / 91s timing
    # drift between the test and the renderer.
    assert "ago" in rendered


def test_hot_list_skips_zero_freq_entries(tmp_path):
    """Tier 2: entries with ``freq <= 0`` are filtered out.

    ``freq=0`` next to a fire emoji ("🔥 skill__foo ×0") is visually
    contradictory. The ranking semantics are "skill has been used N
    times in the relevant window"; a zero count means the skill is no
    longer hot and should not appear under HOT NOW at all.
    """
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    hot = [
        {"qualified_name": "skill__alive", "freq": 3, "last_ts": ""},
        {"qualified_name": "skill__evicted", "freq": 0, "last_ts": ""},
        {"qualified_name": "skill__negative", "freq": -1, "last_ts": ""},
    ]
    rendered, _flat, _ys = render_memory(
        _project_with_empty_memory(tmp_path), cursor=0, hot_list=hot,
    )
    assert "skill__alive" in rendered
    assert "skill__evicted" not in rendered
    assert "skill__negative" not in rendered


def test_other_bucket_entries_render_description(tmp_path):
    """Tier 2: ``OTHER``-bucket entries surface their description line.

    The typed buckets (USER / FEEDBACK / PROJECT / REFERENCE) render
    ``description`` underneath the name when populated. Entries whose
    ``type`` falls outside the four canonical types land in OTHER and
    must use the same render shape — otherwise the OTHER section
    becomes silently lower-fidelity than the typed ones.
    """
    from reyn.tui.widgets.right_panel.memory_tab import render_memory

    # Plant one memory file with type="custom" so it lands in OTHER.
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "custom_thing.md").write_text(
        "---\nname: custom-thing\ndescription: a non-standard memory type\n"
        "metadata:\n  type: custom\n---\n\nbody text\n",
        encoding="utf-8",
    )

    rendered, _flat, _ys = render_memory(
        tmp_path, cursor=0, hot_list=None,
    )
    assert "OTHER" in rendered
    assert "custom-thing" in rendered
    assert "a non-standard memory type" in rendered
