"""Tier 2: Ghost alias registry-existence check at hot-list materialization.

B38 W2 finding: ``_is_valid_qualified_name`` only validates structural shape
(category + separator + entry). A stale alias (e.g. a renamed MCP tool) passes
structural check but is a ghost — it no longer resolves in the current
registry. ``_filter_ghost_names_by_registry`` adds the existence check at
hot-list materialization time, when session registry data is available.

This is additive to structural rejection (``test_hot_list_ghost_alias_rejection.py``).
Names must pass BOTH structural check AND registry-existence check to enter
the hot list.

Test plan:
  R3. valid static-op alias (in KNOWN_STATIC_QUALIFIED_NAMES) passes through.
  R4. ghost static-op (structurally valid category, not in static ops) is filtered.
  R7. mcp.tool ghost (not in mcp_tool_map) is filtered.
  R8. valid mcp.tool (in mcp_tool_map) passes through.
  R11. memory_entry name in known_memory_entries passes through (dynamic
       enumeration from .reyn/memory/*.md).
  R12. memory_entry name NOT in known_memory_entries is filtered (stale name
       from action_usage tracker after the entry was deleted).

No mocks. Uses real _filter_ghost_names_by_registry + real
KNOWN_STATIC_QUALIFIED_NAMES. No RouterLoop instantiation required.
"""
from __future__ import annotations

from reyn.runtime.router_loop import _filter_ghost_names_by_registry
from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

# ── helpers ───────────────────────────────────────────────────────────────────


def _call_filter(
    names: list[str],
    mcp_tool_map: dict | None = None,
    available_agents: list[dict] | None = None,
    known_memory_entries: frozenset[str] | None = None,
) -> list[str]:
    """Convenience wrapper with empty defaults.

    ``known_memory_entries`` defaults to an empty frozenset (= no entries),
    which is the realistic state for tests that aren't exercising the
    memory_entry path. Test-internal default — the production signature
    requires the parameter, see ``_filter_ghost_names_by_registry``.
    """
    return _filter_ghost_names_by_registry(
        names,
        mcp_tool_map=mcp_tool_map,
        available_agents=available_agents,
        known_memory_entries=known_memory_entries if known_memory_entries is not None else frozenset(),
    )


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


# Phase 1 multi_agent collapse (2026-05-25): agent.peer__<name> per-peer
# hot-list alias removed.  Peers are now dispatched generically via
# multi_agent__delegate (operation alias); the dynamic ``to`` enum is
# enriched per-call from available_agents at LLM-call time, so per-peer
# ghost filtering at hot-list-build time is no longer applicable.


# ── R7. mcp.tool ghost filtered ──────────────────────────────────────────────


# Issue #879: the ``mcp.tool__<srv>.<tool>`` per-tool hot-list alias path
# was removed when the MCP surface collapsed to verb actions. Tools are
# now dispatched generically via ``mcp__call_tool``; per-tool ghost
# filtering at the hot-list-build boundary is no longer applicable.


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


# ── R11. memory_entry passes when in known_memory_entries ─────────────────────


def test_r11_memory_entry_passes_when_in_known_set() -> None:
    """Tier 2: memory_entry__<slug> in known_memory_entries passes the filter.

    Regression guard for the 2026-05-17 N4+B38 W2 interaction: PR #138
    seeded dynamic memory_entry aliases into the hot-list, but the B38 W2
    ghost-filter that landed later did not know about dynamic categories.
    Result: all memory_entry aliases were rejected via the static_ops
    fall-through. This test pins the fix that adds the known-set check.
    """
    filtered = _filter_ghost_names_by_registry(
        ["memory_entry__user_project_phoenix"],
        mcp_tool_map=None,
        available_agents=None,
        known_memory_entries=frozenset({"memory_entry__user_project_phoenix"}),
    )
    assert filtered == ["memory_entry__user_project_phoenix"], (
        "memory_entry name in known set must pass the filter."
    )


# ── R12. memory_entry filtered when not in known_memory_entries ───────────────


def test_r12_memory_entry_filtered_when_absent_from_known_set() -> None:
    """Tier 2: memory_entry__<slug> NOT in known_memory_entries is filtered.

    Scenario: user deleted .reyn/memory/<slug>.md between sessions, but
    the action_usage tracker still has it from prior freq history. The
    filter must reject the stale name so the LLM doesn't see a ghost
    alias that would dispatch to a non-existent entry.
    """
    filtered = _filter_ghost_names_by_registry(
        ["memory_entry__deleted_slug"],
        mcp_tool_map=None,
        available_agents=None,
        known_memory_entries=frozenset(),  # zero entries this session
    )
    assert filtered == [], (
        "memory_entry name not in known set must be removed."
    )


