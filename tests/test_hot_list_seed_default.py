"""Tier 2: DEFAULT_HOT_LIST_SEED expansion — B37 W4/W6 fix (Part A).

Verifies that the 4 actions identified in B37 dogfood are present in
DEFAULT_HOT_LIST_SEED and that each resolves via the routing dispatch
(= not a ghost / structurally invalid name).

No mocks. Uses real registry dispatch + real DEFAULT_HOT_LIST_SEED.
"""
from __future__ import annotations

import pytest

from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
from reyn.tools.universal_dispatch import UnknownActionError, resolve_invoke_action


# ── Part A: B37 W4/W6 seed entries present ────────────────────────────────────

# mcp_search — W6 R-WEB scenarios: mcp_search routing miss with fresh
# workspace. Canonical qualified name is skill__mcp_search.
# file__write — W4 S1: LLM used args key "text" instead of "content" because
# the action was absent from the hot list and the D2-wrapper ARS block had no
# entry for it.
# rag.operation__drop_source — W2 S1 / W4 S6: LLM used "source_id" then
# "source_name" across batches; synonym normalization is bankrupt. Seeding
# exposes the canonical {source} schema upfront.
# web__fetch — W3 S4: present in seed since B34; this test asserts it remains.

_B37_REQUIRED_ENTRIES: tuple[str, ...] = (
    "skill__mcp_search",          # W6: mcp_search scenarios refuted on routing miss
    "file__write",                # W4 S1: arg-key hallucination (text vs content)
    "rag.operation__drop_source", # W2 S1 / W4 S6: hallucination drift source_id→source_name
    "web__fetch",                 # W3 S4: web fetch intent cold-start coverage
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
def test_b37_required_entry_resolves_via_dispatch(action_name: str) -> None:
    """Tier 2: each B37 required action resolves via resolve_invoke_action.

    Verifies that the entry is not a ghost: it has a routing rule in
    _OPERATION_RULES or _RESOURCE_RULES (= the dispatch can route it to
    a real handler). This catches bugs where a name is added to the seed
    but has no routing rule and would produce UnknownActionError at invoke
    time.
    """
    try:
        resolved = resolve_invoke_action(action_name, {})
        assert resolved.target_tool_name, (
            f"{action_name!r} resolved but target_tool_name is empty."
        )
    except UnknownActionError as exc:
        pytest.fail(
            f"{action_name!r} does not resolve via resolve_invoke_action: {exc}. "
            f"Either the action name is wrong or a routing rule is missing."
        )
