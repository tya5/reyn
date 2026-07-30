"""Tier 2: DEFAULT_HOT_LIST_SEED expansion — B37 W4/W6 fix (Part A).

Verifies that the 4 actions identified in B37 dogfood are present in
DEFAULT_HOT_LIST_SEED and that each resolves via the routing dispatch
(= not a ghost / structurally invalid name).

No mocks. Uses real registry dispatch + real DEFAULT_HOT_LIST_SEED.
"""
from __future__ import annotations

import pytest

from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
from reyn.tools.universal_dispatch import is_known_action

# ── Part A: B37 W4/W6 seed entries present ────────────────────────────────────

# Issue #879 collapsed surface: skill__mcp_search → mcp_search_registry;
# mcp_install_registry seeded so install requests don't require
# list_actions discovery first.  The 2026-05-25 install 3-verb split
# kept the registry verb as the seeded primary path; install_package /
# install_local are intentionally NOT seeded (= niche flows, list_actions
# discovery is acceptable cost).
# W6 R-WEB scenarios: mcp routing miss with fresh workspace was the
# motivating scenario for seeding the mcp verbs.
# write_file — W4 S1: LLM used args key "text" instead of "content" because
# the action was absent from the hot list and the D2-wrapper ARS block had no
# entry for it.
# 2026-05-25 (post-#898 hot-list seed swap): rag_operation__drop_source
# removed from the required-entries pin. Its B37 schema-hallucination
# protection is now covered by the ARS scope-expansion contract (=
# KNOWN_STATIC_QUALIFIED_NAMES is always in ARS regardless of hot-list,
# per B38; see test_invoke_action_scope_expansion.py::
# test_static_ops_always_present_no_session_state). Seed presence is no
# longer the load-bearing mechanism for that invariant.
# list_mcp_tools + mcp_call_tool — 2026-05-25 walkthrough: cold-start
# "use an installed MCP server" path required discovery via list_actions
# before chaining list_mcp_tools / mcp_call_tool; seeding skips the
# discovery turn.
# web_fetch — W3 S4: present in seed since B34; this test asserts it remains.

_B37_REQUIRED_ENTRIES: tuple[str, ...] = (
    "mcp_search_registry",       # #879/2026-05-25: registry search seed
    "mcp_install_registry",      # #879/2026-05-25: registry install seed
    "list_mcp_tools",            # post-#898: cold-start "use server" verb
    "mcp_call_tool",             # post-#898: cold-start "use server" verb
    "write_file",                # W4 S1: arg-key hallucination (text vs content)
    "web_fetch",                 # W3 S4: web fetch intent cold-start coverage
)


@pytest.mark.parametrize("action_name", _B37_REQUIRED_ENTRIES)
def test_b37_required_entry_present_in_seed(action_name: str) -> None:
    """Tier 2: each B37 required action is present in DEFAULT_HOT_LIST_SEED."""
    assert action_name in DEFAULT_HOT_LIST_SEED, (
        f"{action_name!r} is missing from DEFAULT_HOT_LIST_SEED. "
        f"This action was identified in B37 dogfood as absent from fresh-workspace "
        f"hot lists, causing LLM arg hallucination or routing misses. "
        f"Add it to DEFAULT_HOT_LIST_SEED in src/reyn/tools/action_usage_tracker.py."
    )


@pytest.mark.parametrize("action_name", _B37_REQUIRED_ENTRIES)
def test_b37_required_entry_is_a_known_action(action_name: str) -> None:
    """Tier 2: each B37 required seed entry is a real catalog action.

    Verifies the entry is not a ghost: the hot-list ghost filter drops any
    seed name that is not in the catalog's action set, so a seed entry naming
    a tool that does not exist would be silently dropped rather than
    advertised.
    """
    assert is_known_action(action_name), (
        f"{action_name!r} is not a catalog action, so the hot-list ghost "
        f"filter drops it. Either the name is wrong or the action is missing "
        f"from universal_dispatch._CATEGORY_ACTIONS."
    )
