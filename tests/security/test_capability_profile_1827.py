"""Tier 2: capability profile schema + pure resolver (#1827 S2a).

A ``capability_profile`` resolves into BOTH #1827 axes via one pure resolver:
authority (``ContextualPermission``, rides the S1.5 live ∩-gate) + visibility
(``excluded_categories`` over the 12-entry catalog). This module is unwired —
these pin the schema round-trip, the resolution of each axis, and the
most-restrictive composition.

The round-trip uses a NON-DEFAULT profile (a ``tool_deny`` tool that is not a
default / bridge value) so a silently-wrong load can't pass trivially.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.security.permissions.capability_profile import (
    CapabilityProfile,
    compose_resolved,
    load_capability_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualLayer,
    ContextualPermission,
)
from reyn.tools.universal_catalog import CATEGORIES


def test_load_round_trip_non_default(tmp_path: Path):
    """Tier 2: a non-default profile YAML round-trips into the dataclass."""
    p = tmp_path / "reviewer.yaml"
    p.write_text(textwrap.dedent("""
        name: reviewer
        description: read + memo only
        categories: [file, validation]
        tool_allow: [read_file]
        tool_deny: [memory__write, delegate_to_agent]
    """).lstrip(), encoding="utf-8")

    prof = load_capability_profile(p)

    assert prof.name == "reviewer"
    assert prof.description == "read + memo only"
    assert prof.categories == ("file", "validation")
    assert prof.tool_allow == ("read_file",)
    assert prof.tool_deny == ("memory__write", "delegate_to_agent")


# ── #4004 — a since-renamed tool name in tool_allow/tool_deny fails LOUD ────
# "the name an operator writes IS the name the gate matches" (this module's
# own resolve_profile docstring) means a tool rename is also, silently, a
# rename of what an operator's config must say. Left unguarded, an old name
# would simply stop matching anything — a tool_deny entry silently lifting
# is a permission ESCALATION, the one direction this axis must never fail
# silently in (lead-coder's #4004 review finding, mirroring #3823/#3953's
# _RENAMED_SANDBOX_POLICY_KEYS pattern).


def test_load_rejects_a_renamed_tool_name_in_tool_deny(tmp_path: Path):
    """Tier 2: #4004 — a pre-rename tool name in tool_deny raises loud,
    specific guidance rather than silently loading a deny-list entry that
    will never match anything again."""
    p = tmp_path / "old.yaml"
    p.write_text("name: old\ntool_deny: [agent_spawn]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="renamed to 'spawn_agent'"):
        load_capability_profile(p)


def test_load_rejects_a_renamed_tool_name_in_tool_allow(tmp_path: Path):
    """Tier 2: non-vacuity — the same guard applies to tool_allow, not just
    tool_deny (an old name there silently NARROWS instead of escalating,
    but is still an unannounced state change the operator never asked
    for)."""
    p = tmp_path / "old.yaml"
    p.write_text("name: old\ntool_allow: [session_spawn]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="renamed to 'spawn_session'"):
        load_capability_profile(p)


def test_load_accepts_the_new_tool_name_unchanged(tmp_path: Path):
    """Tier 2: non-vacuity for the guard — the NEW tool name loads normally,
    unaffected by the rename-guard (only the OLD spellings are rejected)."""
    p = tmp_path / "new.yaml"
    p.write_text(
        "name: new\ntool_deny: [spawn_agent, spawn_session, create_topology]\n",
        encoding="utf-8",
    )
    prof = load_capability_profile(p)
    assert prof.tool_deny == ("spawn_agent", "spawn_session", "create_topology")


def test_load_missing_categories_is_none_vs_empty(tmp_path: Path):
    """Tier 2: absent ``categories`` → None (no view narrowing); empty list → ()."""
    p1 = tmp_path / "a.yaml"
    p1.write_text("name: a\ntool_deny: [x]\n", encoding="utf-8")
    assert load_capability_profile(p1).categories is None

    p2 = tmp_path / "b.yaml"
    p2.write_text("name: b\ncategories: []\n", encoding="utf-8")
    assert load_capability_profile(p2).categories == ()


def test_resolve_view_excludes_complement_of_categories():
    """Tier 2: categories → excluded_categories = CATEGORIES − categories."""
    prof = CapabilityProfile(name="r", categories=("file", "web"))
    _contextual, excluded = resolve_profile(prof)
    assert "file" not in excluded and "web" not in excluded
    assert excluded == frozenset(CATEGORIES) - {"file", "web"}
    # every other catalog category is hidden
    assert "memory_operation" in excluded and "exec" in excluded


def test_resolve_no_categories_no_view_narrowing():
    """Tier 2: categories=None → no view narrowing (empty excluded set)."""
    prof = CapabilityProfile(name="r", tool_deny=("memory__write",))
    _contextual, excluded = resolve_profile(prof)
    assert excluded == frozenset()


def test_resolve_enforcement_denies_via_live_gate_type():
    """Tier 2: tool_deny resolves to a ContextualPermission that blocks at the gate.

    The resolved ContextualPermission is the SAME type the live gate
    (router_loop._excluded_result, S1.5) consults — so a denied tool is blocked
    and an allowed one passes (visible ⊆ authorized: the resolver never grants).
    """
    prof = CapabilityProfile(
        name="r", tool_allow=("read_file",), tool_deny=("memory__write",),
    )
    contextual, _excluded = resolve_profile(prof)
    layer = ContextualLayer(contextual)
    assert layer.allows(CapabilityAxis.TOOL, "memory__write") is False  # denied
    assert layer.allows(CapabilityAxis.TOOL, "read_file") is True       # allowed
    # not in the allow-list → narrowed away (allow-list semantics)
    assert layer.allows(CapabilityAxis.TOOL, "web_search") is False


def test_compose_union_deny_intersect_allow_union_excluded():
    """Tier 2: compose = most-restrictive (∪ deny, ∩ allow, ∪ excluded)."""
    a = (
        ContextualPermission(tool_allow=frozenset({"x", "y"}), tool_deny=frozenset({"d1"})),
        frozenset({"file"}),
    )
    b = (
        ContextualPermission(tool_allow=frozenset({"y", "z"}), tool_deny=frozenset({"d2"})),
        frozenset({"web"}),
    )
    contextual, excluded = compose_resolved([a, b])
    assert contextual.tool_deny == frozenset({"d1", "d2"})        # union
    assert contextual.tool_allow == frozenset({"y"})             # intersection
    assert excluded == frozenset({"file", "web"})               # union


def test_compose_none_allow_is_top():
    """Tier 2: a None (⊤) allow-list does not constrain the composed allow."""
    a = (ContextualPermission(tool_allow=None, tool_deny=frozenset({"d1"})), frozenset())
    b = (ContextualPermission(tool_allow=frozenset({"x"}), tool_deny=frozenset()), frozenset())
    contextual, _ = compose_resolved([a, b])
    # only b constrains the allow-list → effective allow = {x}
    assert contextual.tool_allow == frozenset({"x"})
    assert contextual.tool_deny == frozenset({"d1"})


def test_compose_empty_is_inert():
    """Tier 2: composing nothing yields an inert (⊤ allow, ∅ deny, ∅ excluded)."""
    contextual, excluded = compose_resolved([])
    assert contextual.tool_allow is None
    assert contextual.tool_deny == frozenset()
    assert excluded == frozenset()
