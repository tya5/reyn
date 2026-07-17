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
  R8. #3026 — a legacy DOTTED mcp.tool name is filtered regardless of
      mcp_tool_map: ``mcp.tool`` stopped being a category at #879.
  R11. #3026 — a memory_entry name is ALWAYS filtered now: the category is
       collapsed, so no such name resolves and none is ever seeded.
  R12. memory_entry name is filtered (stale name from the action_usage
       tracker after the entry was deleted / after the #3026 collapse).

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
) -> list[str]:
    """Convenience wrapper with empty defaults.

    #3026 removed the ``known_memory_entries`` parameter along with the
    per-session ``.reyn/memory/*.md`` enumeration that fed it — the static op
    registry is now the complete action set, so the filter needs no
    dynamic-category side-channel.
    """
    return _filter_ghost_names_by_registry(
        names,
        mcp_tool_map=mcp_tool_map,
        available_agents=available_agents,
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


def test_r8_legacy_dotted_mcp_tool_name_filtered_even_if_in_tool_map() -> None:
    """Tier 2: #3026 — a legacy dotted ``mcp.tool__<server>.<tool>`` name is
    filtered even when it is present in mcp_tool_map.

    This test used to assert the opposite. Both the old expectation and the old
    pass were artifacts: ``mcp.tool`` ceased to be a category at #879 (collapsed
    into ``mcp``), so the name has not parsed since — it never reached the
    mcp_tool_map branch this test believed it was exercising. It reached the
    unparseable branch, which passed it through. #3026 makes that branch DROP
    instead, because a name that cannot resolve must not become a function name;
    the outcome is now correct for the right reason. (Belt-and-braces: the #1456
    wire-grammar guard in _build_hot_list_aliases would also reject the dot.)
    """
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

    assert result == [], "a name from a category collapsed at #879 cannot resolve"


# ── R11. memory_entry passes when in known_memory_entries ─────────────────────


def test_r11_memory_entry_always_filtered_after_collapse() -> None:
    """Tier 2: #3026 — a memory_entry__<slug> name is filtered even though the
    matching .md file exists on disk.

    The N4 mechanism (2026-05-17) seeded one hot-list alias per shared memory
    file, and this test used to pin that such an alias SURVIVES the ghost filter.
    #3026 collapsed the memory_entry category — the name no longer resolves, so
    letting it through would emit a function name the dispatcher rejects.
    Reading a memory is now memory_operation__read (which also reaches the AGENT
    layer the alias never could). The filter needs no special case to get this
    right: with no dynamic categories left, the static op registry is the whole
    action set and a memory_entry name simply is not in it.
    """
    filtered = _call_filter(["memory_entry__user_project_phoenix"])
    assert filtered == [], (
        "a collapsed-category name must never reach the wire (#3026)"
    )


# ── R12. memory_entry filtered when not in known_memory_entries ───────────────


def test_r12_stale_tracker_name_filtered() -> None:
    """Tier 2: a stale memory_entry__<slug> from the action_usage tracker's freq
    history is filtered.

    Scenario (unchanged by #3026, and now also the upgrade path): the tracker's
    on-disk freq table survives across sessions, so it can still name actions from
    before the entry was deleted — or from before the category was collapsed. The
    filter must reject them so the LLM never sees an alias that cannot dispatch.
    """
    filtered = _call_filter(["memory_entry__deleted_slug"])
    assert filtered == [], (
        "a name absent from the registry must be removed."
    )


