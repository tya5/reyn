"""Tier 2: #2032 — task ops reachable via ENUMERATION (the missing-half of #2026).

#2026 wired the task category into the DISPATCH (universal_dispatch._OPERATION_RULES,
the invoke_action routing) but NOT into the ENUMERATION (the static-category tuple
in `_enumerate_category`). So the 11 task ops resolved via invoke_action but were
INVISIBLE to the catalog — the construction-forwarding-gap class: dispatch wired,
enumeration not.

`_enumerate_category` is the single-source enumeration feeding BOTH the flat
catalog (`catalog_entries`, the enumerate-all projection = DEFECT A, the
production-default reachability) AND `list_actions` (= DEFECT B). The single seam
(adding "task" to the static-category tuple) fixes both — proven here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.tools.types import RouterCallerState
from reyn.tools.universal_catalog import _handle_list_actions, catalog_entries

_TASK_OPS = {
    "task__create", "task__update_status", "task__get", "task__list",
    "task__add_dependency", "task__remove_dependency", "task__repoint_dependency",
    "task__abort", "task__heartbeat", "task__register_unblock_predicate",
    "task__comment",
}


def _ctx() -> SimpleNamespace:
    # task is a STATIC category (enumerates from _OPERATION_RULES, no
    # router_state.available_*). The other categories catalog_entries walks
    # (skill/agent/mcp) DO consult router_state — give empty defaults so the
    # full-catalog walk runs (they enumerate to nothing) and `task` is the
    # subject under test.
    # A real RouterCallerState has every field with safe defaults (None / []),
    # so the non-task category walks enumerate to empty without AttributeError.
    return SimpleNamespace(router_state=RouterCallerState())


def test_enumerate_all_flat_catalog_includes_the_11_task_ops():
    """Tier 2: DEFECT A — the flat catalog (`catalog_entries`, the enumerate-all
    projection any scheme renders) includes all 11 task ops, so they are reachable
    on the production-default scheme, not just via the invoke_action dispatch."""
    names = {e["name"] for e in catalog_entries(_ctx())}
    missing = _TASK_OPS - names
    assert not missing, f"task ops missing from the flat catalog: {sorted(missing)}"


@pytest.mark.asyncio
async def test_list_actions_task_category_returns_the_11():
    """Tier 2: DEFECT B — list_actions(category=['task']) returns the 11 task ops
    (not empty). Pure _OPERATION_RULES lookup, no embedding — env-independent
    (resolves tui's Stage-2 caveat by the root cause)."""
    res = await _handle_list_actions({"category": ["task"], "limit": 50}, _ctx())
    qns = {i["qualified_name"] for i in res["items"]}
    missing = _TASK_OPS - qns
    assert not missing, f"task ops missing from list_actions(task): {sorted(missing)}"
    # narrowed browse returns ONLY task ops (no bleed from other categories)
    assert qns == _TASK_OPS, f"unexpected non-task entries: {sorted(qns - _TASK_OPS)}"
