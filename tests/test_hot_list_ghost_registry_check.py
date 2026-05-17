"""Tier 2: Ghost alias registry-existence check at hot-list materialization.

B38 W2 finding: ``_is_valid_qualified_name`` only validates structural shape
(category + separator + entry). A renamed skill like ``skill__create_skill``
passes structural check but is a ghost — the skill no longer exists under
that name. ``_filter_ghost_names_by_registry`` adds the existence check at
hot-list materialization time, when session registry data is available.

This is additive to structural rejection (``test_hot_list_ghost_alias_rejection.py``).
Names must pass BOTH structural check AND registry-existence check to enter
the hot list.

Test plan:
  R1. skill ghost (passes structural, absent from skill_meta_map) is filtered.
  R2. valid skill alias (present in skill_meta_map) passes through.
  R3. valid static-op alias (in KNOWN_STATIC_QUALIFIED_NAMES) passes through.
  R4. ghost static-op (structurally valid category, not in static ops) is filtered.
  R5. agent.peer ghost (not in available_agents) is filtered.
  R6. valid agent.peer (in available_agents) passes through.
  R7. mcp.tool ghost (not in mcp_tool_map) is filtered.
  R8. valid mcp.tool (in mcp_tool_map) passes through.
  R9. Warning logged once per unique ghost name (deduplication).
  R10. Integration: ActionUsageTracker freq-loaded jsonl with 1 valid skill +
       1 ghost skill → hot-list excludes ghost.
  R11. memory.entry name in known_memory_entries passes through (dynamic
       enumeration from .reyn/memory/*.md).
  R12. memory.entry name NOT in known_memory_entries is filtered (stale name
       from action_usage tracker after the entry was deleted).

No mocks. Uses real _filter_ghost_names_by_registry + real ActionUsageTracker
+ real KNOWN_STATIC_QUALIFIED_NAMES. No RouterLoop instantiation required.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from reyn.chat.router_loop import _filter_ghost_names_by_registry
from reyn.tools.action_usage_tracker import ActionUsageTracker
from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

# ── helpers ───────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _call_filter(
    names: list[str],
    skill_meta_map: dict | None = None,
    mcp_tool_map: dict | None = None,
    available_agents: list[dict] | None = None,
    known_memory_entries: frozenset[str] | None = None,
) -> list[str]:
    """Convenience wrapper with empty defaults.

    ``known_memory_entries`` defaults to an empty frozenset (= no entries),
    which is the realistic state for tests that aren't exercising the
    memory.entry path. Test-internal default — the production signature
    requires the parameter, see ``_filter_ghost_names_by_registry``.
    """
    return _filter_ghost_names_by_registry(
        names,
        skill_meta_map=skill_meta_map,
        mcp_tool_map=mcp_tool_map,
        available_agents=available_agents,
        known_memory_entries=known_memory_entries if known_memory_entries is not None else frozenset(),
    )


# ── R1. skill ghost filtered ──────────────────────────────────────────────────


def test_r1_skill_ghost_filtered_when_absent_from_skill_meta_map() -> None:
    """Tier 2: skill ghost absent from skill_meta_map is removed from hot list.

    B38 W2 root cause: skill__create_skill was renamed to skill__skill_builder.
    The old name passes structural check (category=skill, entry=create_skill)
    but the skill no longer exists. With registry check it must be removed.
    """
    skill_meta_map = {
        "skill__skill_builder": {"description": "Build skills", "input_schema": {}},
    }
    result = _call_filter(
        ["skill__create_skill", "skill__skill_builder"],
        skill_meta_map=skill_meta_map,
    )

    assert "skill__skill_builder" in result, (
        "Canonical skill__skill_builder must pass registry check."
    )
    assert "skill__create_skill" not in result, (
        "Ghost skill__create_skill (renamed; absent from skill_meta_map) must be filtered."
    )


# ── R2. valid skill alias passes ─────────────────────────────────────────────


def test_r2_valid_skill_alias_passes_registry_check() -> None:
    """Tier 2: a skill alias present in skill_meta_map passes registry check."""
    skill_meta_map = {
        "skill__word_stats_demo": {
            "description": "Word statistics demo",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }
    result = _call_filter(["skill__word_stats_demo"], skill_meta_map=skill_meta_map)

    assert "skill__word_stats_demo" in result


# ── R3. valid static-op alias passes ─────────────────────────────────────────


def test_r3_valid_static_op_passes_registry_check() -> None:
    """Tier 2: a name in KNOWN_STATIC_QUALIFIED_NAMES passes registry check."""
    # Pick a known static op; verify it's still in the static set defensively.
    static_name = "file__read"
    assert static_name in KNOWN_STATIC_QUALIFIED_NAMES, (
        f"{static_name!r} must be in KNOWN_STATIC_QUALIFIED_NAMES for this test to be valid."
    )
    result = _call_filter([static_name])

    assert static_name in result, (
        f"{static_name!r} is a static op and must pass registry check."
    )


# ── R4. ghost static-op filtered ─────────────────────────────────────────────


def test_r4_structurally_valid_nonexistent_op_filtered() -> None:
    """Tier 2: a structurally valid name not in static ops or any registry is filtered.

    Example: file__nonexistent_op_xyz — valid category 'file', valid separator,
    non-empty entry; but not in KNOWN_STATIC_QUALIFIED_NAMES.
    """
    ghost = "file__nonexistent_op_xyz"
    assert ghost not in KNOWN_STATIC_QUALIFIED_NAMES, (
        f"Precondition: {ghost!r} must not exist in static ops."
    )
    result = _call_filter([ghost])

    assert ghost not in result, (
        f"Ghost {ghost!r} not in any registry must be filtered."
    )


# ── R5. agent.peer ghost filtered ────────────────────────────────────────────


def test_r5_agent_peer_ghost_filtered_when_absent_from_agents() -> None:
    """Tier 2: agent.peer ghost absent from available_agents is filtered."""
    available_agents = [{"name": "researcher", "role": "Research specialist"}]
    result = _call_filter(
        ["agent.peer__researcher", "agent.peer__nonexistent_agent_xyz"],
        available_agents=available_agents,
    )

    assert "agent.peer__researcher" in result
    assert "agent.peer__nonexistent_agent_xyz" not in result, (
        "Ghost peer agent not in available_agents must be filtered."
    )


# ── R6. valid agent.peer passes ──────────────────────────────────────────────


def test_r6_valid_agent_peer_passes_registry_check() -> None:
    """Tier 2: agent.peer alias present in available_agents passes registry check."""
    available_agents = [{"name": "dogfood-runner", "role": "Dogfood execution"}]
    result = _call_filter(
        ["agent.peer__dogfood-runner"],
        available_agents=available_agents,
    )

    assert "agent.peer__dogfood-runner" in result


# ── R7. mcp.tool ghost filtered ──────────────────────────────────────────────


def test_r7_mcp_tool_ghost_filtered_when_absent_from_mcp_tool_map() -> None:
    """Tier 2: mcp.tool ghost absent from mcp_tool_map is filtered."""
    mcp_tool_map = {
        "mcp.tool__brave.search": {"description": "Brave search", "input_schema": {}},
    }
    result = _call_filter(
        ["mcp.tool__brave.search", "mcp.tool__ghost_server.ghost_tool"],
        mcp_tool_map=mcp_tool_map,
    )

    assert "mcp.tool__brave.search" in result
    assert "mcp.tool__ghost_server.ghost_tool" not in result, (
        "Ghost mcp.tool not in mcp_tool_map must be filtered."
    )


# ── R8. valid mcp.tool passes ────────────────────────────────────────────────


def test_r8_valid_mcp_tool_passes_registry_check() -> None:
    """Tier 2: mcp.tool present in mcp_tool_map passes registry check."""
    mcp_tool_map = {
        "mcp.tool__github.search_code": {
            "description": "Search GitHub code",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
    result = _call_filter(
        ["mcp.tool__github.search_code"],
        mcp_tool_map=mcp_tool_map,
    )

    assert "mcp.tool__github.search_code" in result


# ── R9. warning logged once per unique ghost ──────────────────────────────────


def test_r9_ghost_warning_logged_once_per_unique_name(capsys: pytest.CaptureFixture) -> None:
    """Tier 2: rejection warning is emitted to stderr once per unique ghost alias.

    Calling filter twice with the same ghost using the same _warned set must
    produce only 1 warning (= session-level deduplication).
    """
    warned: set[str] = set()
    ghost = "skill__nonexistent_ghost_xyz"
    skill_meta_map: dict = {}

    # First call: warning emitted.
    _filter_ghost_names_by_registry(
        [ghost], skill_meta_map=skill_meta_map, mcp_tool_map=None, available_agents=None,
        known_memory_entries=frozenset(),
        _warned=warned,
    )
    captured = capsys.readouterr()
    assert ghost in captured.err, (
        "First encounter of ghost must emit a warning to stderr."
    )
    first_count = captured.err.count(ghost)

    # Second call with same _warned: no additional warning.
    _filter_ghost_names_by_registry(
        [ghost], skill_meta_map=skill_meta_map, mcp_tool_map=None, available_agents=None,
        known_memory_entries=frozenset(),
        _warned=warned,
    )
    captured2 = capsys.readouterr()
    assert ghost not in captured2.err, (
        "Second encounter of same ghost with shared _warned must NOT emit warning."
    )


# ── R10. Integration: tracker + filter ───────────────────────────────────────


def test_r10_integration_tracker_ghost_excluded_from_hot_list(tmp_path: Path) -> None:
    """Tier 2: integration — ActionUsageTracker freq history with ghost skill
    is filtered at hot-list build time using real registry data.

    Setup:
    - JSONL has 1 valid skill alias (skill__word_stats_demo) and 1 ghost
      skill alias (skill__nonexistent_xyz) recorded with equal frequency.
    - skill_meta_map contains only skill__word_stats_demo (= registry has
      only that skill).
    - _filter_ghost_names_by_registry is called on get_top_n() output with
      the real skill_meta_map.

    Assert: result contains skill__word_stats_demo, not skill__nonexistent_xyz.
    """
    now = time.time()
    persist_path = tmp_path / "action_usage.jsonl"
    _write_jsonl(persist_path, [
        {"qualified_name": "skill__word_stats_demo", "ts": now},
        {"qualified_name": "skill__word_stats_demo", "ts": now},  # freq=2
        {"qualified_name": "skill__nonexistent_xyz", "ts": now},  # freq=1, ghost
    ])

    tracker = ActionUsageTracker(persist_path=persist_path)

    # Both names pass structural check and are loaded into tracker.
    top_names = tracker.get_top_n(10, seed=[])
    assert "skill__word_stats_demo" in top_names, (
        "Precondition: tracker must load skill__word_stats_demo."
    )
    assert "skill__nonexistent_xyz" in top_names, (
        "Precondition: tracker must load ghost skill__nonexistent_xyz (structural check passes)."
    )

    # Registry knows only skill__word_stats_demo.
    skill_meta_map = {
        "skill__word_stats_demo": {
            "description": "Word statistics demo",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            "input_wrapped": True,
        }
    }

    # Apply registry-existence filter.
    filtered = _filter_ghost_names_by_registry(
        top_names,
        skill_meta_map=skill_meta_map,
        mcp_tool_map=None,
        available_agents=None,
        known_memory_entries=frozenset(),
    )

    assert "skill__word_stats_demo" in filtered, (
        "Valid skill must survive registry check."
    )
    assert "skill__nonexistent_xyz" not in filtered, (
        "Ghost skill not in skill_meta_map must be removed by registry check."
    )


# ── R11. memory.entry passes when in known_memory_entries ─────────────────────


def test_r11_memory_entry_passes_when_in_known_set() -> None:
    """Tier 2: memory.entry__<slug> in known_memory_entries passes the filter.

    Regression guard for the 2026-05-17 N4+B38 W2 interaction: PR #138
    seeded dynamic memory.entry aliases into the hot-list, but the B38 W2
    ghost-filter that landed later did not know about dynamic categories.
    Result: all memory.entry aliases were rejected via the static_ops
    fall-through. This test pins the fix that adds the known-set check.
    """
    filtered = _filter_ghost_names_by_registry(
        ["memory.entry__user_project_phoenix"],
        skill_meta_map=None,
        mcp_tool_map=None,
        available_agents=None,
        known_memory_entries=frozenset({"memory.entry__user_project_phoenix"}),
    )
    assert filtered == ["memory.entry__user_project_phoenix"], (
        "memory.entry name in known set must pass the filter."
    )


# ── R12. memory.entry filtered when not in known_memory_entries ───────────────


def test_r12_memory_entry_filtered_when_absent_from_known_set() -> None:
    """Tier 2: memory.entry__<slug> NOT in known_memory_entries is filtered.

    Scenario: user deleted .reyn/memory/<slug>.md between sessions, but
    the action_usage tracker still has it from prior freq history. The
    filter must reject the stale name so the LLM doesn't see a ghost
    alias that would dispatch to a non-existent entry.
    """
    filtered = _filter_ghost_names_by_registry(
        ["memory.entry__deleted_slug"],
        skill_meta_map=None,
        mcp_tool_map=None,
        available_agents=None,
        known_memory_entries=frozenset(),  # zero entries this session
    )
    assert filtered == [], (
        "memory.entry name not in known set must be removed."
    )


