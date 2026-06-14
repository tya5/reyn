"""Tier 1: #1593 catalog_entries — flat generic action-projection contract.

``catalog_entries(ctx)`` is the substrate behind ``SchemeOps.catalog_entries``:
every usable action as a flat ``{name, description, parameters}`` dict (the actions
exposed, the 13-category structure hidden = the P7 boundary). It is built from the
SAME ``_enumerate_category`` + ``_describe_one`` machinery ``list_actions`` /
``describe_action`` use, so all agree BY CONSTRUCTION (#1455 list ≡ describe).

Pins: flat shape + schema-completeness bar (``parameters`` never None) + name sort
+ availability-gating on ``router_state`` + the single-source invariant vs
``describe_action``.

Real ToolContext + RouterCallerState with a stub host; no mocks of collaborators.
"""
from __future__ import annotations

import asyncio

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action, catalog_entries


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """Minimal host whose list_available_skills returns the D2-full catalogue
    shape (input_schema per entry) so resource-category enrichment resolves."""

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
        router_state=RouterCallerState(
            host=_FakeHost(sk), available_skills=sk, mcp_servers=None,
        ),
    )


def test_flat_shape_and_completeness_bar():
    """Tier 1: every entry is exactly {name, description, parameters}, parameters a dict (never None)."""
    entries = catalog_entries(_ctx())
    assert entries, "static categories alone should yield entries"
    for e in entries:
        assert set(e) == {"name", "description", "parameters"}, f"flat generic shape: {e}"
        assert isinstance(e["name"], str) and "__" in e["name"], f"name is qualified: {e['name']}"
        assert isinstance(e["parameters"], dict), f"completeness bar (never None): {e['name']}"
        assert isinstance(e["description"], str)


def test_sorted_by_name():
    """Tier 1: deterministic name sort (stable tools= ordering → replay-fixture stability)."""
    names = [e["name"] for e in catalog_entries(_ctx())]
    assert names == sorted(names)


def test_resource_categories_gated_on_router_state():
    """Tier 1: a skill in router_state surfaces skill__X with its input_schema as parameters."""
    entries = catalog_entries(_ctx(skills=[_SKILL]))
    by_name = {e["name"]: e for e in entries}
    assert "skill__code_review" in by_name, "resource category enumerated from router_state"
    params = by_name["skill__code_review"]["parameters"]
    assert params.get("properties", {}).keys() >= {"diff", "focus"}, "skill input_schema projected"


def test_empty_router_state_keeps_static_drops_resources():
    """Tier 1: without router_state skills, resource cats drop; static cats survive ("usable" semantics)."""
    entries = catalog_entries(_ctx(skills=[]))
    names = {e["name"] for e in entries}
    assert any(n.startswith("file__") for n in names), "static categories survive"
    assert not any(n.startswith("skill__") for n in names), "no skills → no skill__ entries"


def test_single_source_invariant_vs_describe_action():
    """Tier 1: #1455 — catalog_entries' description/parameters for an action == describe_action's
    description/input_schema (both via the shared _describe_one), by construction."""
    ctx = _ctx(skills=[_SKILL])
    by_name = {e["name"]: e for e in catalog_entries(ctx)}
    for name in ("file__edit", "skill__code_review"):
        entry = by_name[name]
        described = asyncio.run(_handle_describe_action({"action_name": name}, ctx))
        # Both actions carry a real dict schema, so the completeness bar is a no-op
        # here and equality holds directly.
        assert entry["description"] == described["description"], f"{name}: description single-source"
        assert entry["parameters"] == described["input_schema"], f"{name}: schema single-source"
