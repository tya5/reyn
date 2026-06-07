"""Tier 1: #187 Stage B — list_actions returns selection-grade items.

When the browse is narrowed to a category, each list_actions item carries the
full ``description`` + ``input_schema`` that describe_action returns (name +
description + schema), so the model can SELECT an action without a separate
describe_action round-trip — which weak models rarely make. This inherits the
schema-blind-hallucination protection the removed ARS block provided. The
unfiltered alphabetical browse stays compact (breadth scan). The shared
``_describe_one`` core guarantees ``list ≡ describe`` BY CONSTRUCTION.

Real ToolContext + RouterCallerState with a stub host; no mocks of collaborators.
"""
from __future__ import annotations

import asyncio

from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    _describe_one,
    _handle_describe_action,
    _handle_list_actions,
)


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """Minimal host: list_available_skills returns the D2-full catalogue shape
    (input_schema per entry) so resource-category enrichment resolves."""

    def __init__(self, skills):
        self._skills = skills

    def list_available_skills(self):
        return list(self._skills)


_SKILL = {
    "name": "code_review",
    "description": "Review a code diff and report issues.",
    "input_fields": ["diff", "focus"],
    "input_schema": {
        "type": "object",
        "properties": {"diff": {"type": "string"}, "focus": {"type": "string"}},
        "required": ["diff"],
    },
    "input_wrapped": True,
}


def _ctx(skills=None) -> ToolContext:
    sk = skills or []
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        # available_skills feeds list_actions enumeration; host.list_available_skills
        # feeds describe_action / _describe_one resolution — set both so list ≡ describe.
        router_state=RouterCallerState(
            host=_FakeHost(sk), available_skills=sk, mcp_servers=None,
        ),
    )


def _list(ctx: ToolContext, **args) -> dict:
    return asyncio.run(_handle_list_actions(args, ctx))


def _describe(ctx: ToolContext, name: str) -> dict:
    return asyncio.run(_handle_describe_action({"action_name": name}, ctx))


def test_narrowed_list_item_carries_description_and_schema():
    """Tier 1: a category-narrowed list_actions item carries full description + input_schema."""
    ctx = _ctx()
    items = _list(ctx, category=["file"])["items"]
    fe = next(i for i in items if i["qualified_name"] == "file__edit")
    assert fe.get("description"), "narrowed item must carry a full description"
    assert fe.get("input_schema", {}).get("properties"), "narrowed item must carry input_schema"


def test_static_op_and_resource_both_enriched():
    """Tier 1: both a static op (file__edit) and a resource (skill__X) get input_schema."""
    ctx = _ctx(skills=[_SKILL])
    file_items = _list(ctx, category=["file"])["items"]
    fe = next(i for i in file_items if i["qualified_name"] == "file__edit")
    assert fe["input_schema"]["properties"], "static op must carry its parameters schema"

    skill_items = _list(ctx, category=["skill"])["items"]
    sk = next(i for i in skill_items if i["qualified_name"] == "skill__code_review")
    assert set(sk["input_schema"]["properties"]) == {"diff", "focus"}, (
        "resource (skill) must carry its per-resource schema, not the dispatcher envelope"
    )


def test_unfiltered_browse_stays_compact():
    """Tier 1: the unfiltered (all-category) browse omits input_schema — compact breadth scan."""
    ctx = _ctx()
    items = _list(ctx)["items"]
    assert items, "expected a non-empty browse page"
    assert all("input_schema" not in i for i in items), (
        "the unfiltered browse must stay compact (no per-item schema)"
    )


def test_list_equals_describe_by_construction():
    """Tier 1: ★load-bearing invariant — a narrowed list item's description +
    input_schema EQUAL describe_action's for the same action (shared _describe_one).
    Guards against future drift if either path bypasses the helper."""
    ctx = _ctx(skills=[_SKILL])
    for cat, name in [("file", "file__edit"), ("skill", "skill__code_review")]:
        item = next(
            i for i in _list(ctx, category=[cat])["items"]
            if i["qualified_name"] == name
        )
        d = _describe(ctx, name)
        assert item["description"] == d["description"], f"{name}: description must match describe_action"
        assert item["input_schema"] == d["input_schema"], f"{name}: input_schema must match describe_action"


def test_describe_one_returns_none_for_unresolvable():
    """Tier 1: _describe_one returns None for an unresolvable name — so list_actions
    skips enrichment for such an item gracefully (keeps short_description, no crash)."""
    assert _describe_one("bogus_category__nope", _ctx(), get_default_registry()) is None
