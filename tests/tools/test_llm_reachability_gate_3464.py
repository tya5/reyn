"""Tier 2: registry <-> LLM-reachability bidirectional gate (#3464).

#2032 / #3083 / #2913 / #2875 / #3215 each individually closed one instance
of "registered router=allow but the LLM can never actually reach it." #3464
is the 6th instance (cron: 5 tools) and asks for a gate instead of a 7th
patch. This suite exercises ``reyn.tools.llm_reachability``:

  1. Every ``router=allow`` tool the gate finds unreachable today is
     declared in ``UNREACHABLE_TOOL_REASONS`` with a closed-vocabulary
     classification + non-empty reason (unreachable subseteq declared).
  2. Every declared entry really is unreachable (declared subseteq
     unreachable) -- an entry cannot survive after someone fixes the
     wiring; that would silently hide the fix's own regression coverage.
  3. Non-vacuity: strip-falsify each route independently and confirm the
     gate's identity check would go RED for an undeclared tool.

No mocks -- everything here reads the real default ToolRegistry /
``KNOWN_ACTION_NAMES`` / ``build_tools`` source, or passes a plain string /
set override to the module's own testability seams (``source_text`` /
``action_names`` / ``allow_names``).

#3429 (merged alongside): route (b) is restated from "bare dispatch target of
an ``_OPERATION_RULES`` qualified name" to "member of the catalog action set"
-- the same route in the one-name world, measured to produce the same
unreachable set at the switch-over.
"""
from __future__ import annotations

import inspect

import pytest

from reyn.tools.llm_reachability import (
    UNREACHABLE_CLASSIFICATIONS,
    UNREACHABLE_TOOL_REASONS,
    compute_direct_advertisable_tool_names,
    compute_invoke_action_reachable_tool_names,
    compute_router_allow_tool_names,
    compute_unreachable_router_allow_tool_names,
)


def _assert_reachability_gate(unreachable: frozenset[str]) -> None:
    """The gate's own identity check, factored out so both the real-state
    test and the strip-falsify tests exercise the identical assertion."""
    declared = frozenset(UNREACHABLE_TOOL_REASONS)
    undeclared = unreachable - declared
    assert not undeclared, (
        f"router=allow tool(s) {sorted(undeclared)} are not reachable via "
        f"build_tools() direct advertisement NOR invoke_action, and are not "
        f"declared in UNREACHABLE_TOOL_REASONS -- register them with a "
        f"reason (see src/reyn/tools/llm_reachability.py), or wire them into "
        f"reachability if that was an oversight (#3464)."
    )
    stale = declared - unreachable
    assert not stale, (
        f"UNREACHABLE_TOOL_REASONS entries {sorted(stale)} are declared "
        f"unreachable but the census now finds them reachable -- remove the "
        f"stale entry (its wiring fix should ship WITH the removal so the "
        f"regression stays covered, not silently)."
    )


def test_current_unreachable_set_matches_the_declared_registry() -> None:
    """Tier 2: registry allow-set minus (build_tools union invoke_action)
    equals exactly UNREACHABLE_TOOL_REASONS' keys, in both directions."""
    unreachable = compute_unreachable_router_allow_tool_names()
    _assert_reachability_gate(unreachable)


def test_declared_reasons_use_the_closed_classification_and_are_nonempty() -> None:
    """Tier 2: no entry may register a hole without a real classification +
    a reason with actual content (comments.md-style -- must say what the
    hole IS, not just that it exists)."""
    assert UNREACHABLE_TOOL_REASONS, "the registry should not be empty while cron is open"
    for name, entry in UNREACHABLE_TOOL_REASONS.items():
        assert entry.classification in UNREACHABLE_CLASSIFICATIONS, (
            f"{name!r} has classification {entry.classification!r}, not one "
            f"of {sorted(UNREACHABLE_CLASSIFICATIONS)}"
        )
        assert isinstance(entry.reason, str) and len(entry.reason.strip()) >= 40, (
            f"{name!r}'s reason is missing or too short to be a real "
            f"falsifiable explanation: {entry.reason!r}"
        )


def test_cron_tools_are_declared_pending_capability_decision() -> None:
    """Tier 2: the 5 cron tools from #3464's own measurement are present
    and classified as a capability decision, not a wiring bug -- adding
    them would grant the LLM a new capability (owner call), unlike the
    other declared entries."""
    cron_names = {"cron_register", "cron_unregister", "cron_list", "cron_enable", "cron_disable"}
    assert cron_names <= set(UNREACHABLE_TOOL_REASONS)
    for name in cron_names:
        assert UNREACHABLE_TOOL_REASONS[name].classification == "PENDING_CAPABILITY_DECISION"


def test_registered_router_allow_tools_all_come_from_the_real_registry() -> None:
    """Tier 2: sanity check that the census population carries a known
    router=allow tool and excludes router=deny tools (e.g. ask_user is
    CLI/internal-only by design and must never appear in this gate's
    population)."""
    allow_names = compute_router_allow_tool_names()
    assert "web_search" in allow_names, "sanity: web_search is a stable router=allow tool"
    assert "ask_user" not in allow_names, "ask_user is gates.router=deny and out of scope by construction"


# ── Non-vacuity: strip-falsify each route independently ────────────────────


def test_strip_direct_advertisement_route_makes_the_gate_fire() -> None:
    """Tier 2: non-vacuity -- removing a tool ONLY reachable via route (a)
    (direct build_tools() advertisement, never routed through
    invoke_action) from the AST census must produce an undeclared
    unreachable tool -- proving the identity check in
    ``test_current_unreachable_set_matches_the_declared_registry`` would go
    RED if this ever happened for real, instead of the assertion being
    vacuously satisfied.

    ``create_topology`` is confirmed (asserted below against the live set)
    to not be a catalog action -- it is direct-route-only, so stripping its
    lookup call site removes it from BOTH routes' union, unlike stripping
    e.g. ``web_search`` (a catalog action) which would still be reachable
    through route (b) alone.
    """
    from reyn.runtime.router_tools import build_tools

    real_source = inspect.getsource(build_tools)
    anchor = '_registry.lookup("create_topology")'
    assert real_source.count(anchor) == 1, (
        "strip anchor must be unique or this falsifies the wrong call site"
    )
    stripped_source = real_source.replace(anchor, '_registry.lookup("__stripped_3464__")')

    invoke_reachable = compute_invoke_action_reachable_tool_names()
    assert "create_topology" not in invoke_reachable, (
        "create_topology must be direct-route-only for this strip to be a valid probe"
    )

    baseline_unreachable = compute_unreachable_router_allow_tool_names()
    assert "create_topology" not in baseline_unreachable

    stripped_unreachable = compute_unreachable_router_allow_tool_names(source_text=stripped_source)
    assert "create_topology" in stripped_unreachable

    with pytest.raises(AssertionError):
        _assert_reachability_gate(stripped_unreachable)


def test_strip_invoke_action_route_makes_the_gate_fire() -> None:
    """Tier 2: non-vacuity -- removing a tool ONLY reachable via route (b)
    (invoke_action / _OPERATION_RULES, never directly advertised by
    build_tools()) must equally produce an undeclared unreachable tool --
    proving route (b) is load-bearing in the gate, not decorative.

    ``search_knowledge`` is confirmed to have no direct
    ``_registry.lookup("search_knowledge")`` call site in ``build_tools`` --
    it is invoke-route-only (the ``knowledge`` category's single action).
    """
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

    direct_reachable = compute_direct_advertisable_tool_names()
    assert "search_knowledge" not in direct_reachable, (
        "search_knowledge must be invoke-route-only for this strip to be a valid probe"
    )

    baseline_unreachable = compute_unreachable_router_allow_tool_names()
    assert "search_knowledge" not in baseline_unreachable

    stripped_names = frozenset(KNOWN_ACTION_NAMES - {"search_knowledge"})
    stripped_unreachable = compute_unreachable_router_allow_tool_names(action_names=stripped_names)
    assert "search_knowledge" in stripped_unreachable

    with pytest.raises(AssertionError):
        _assert_reachability_gate(stripped_unreachable)


def test_a_hypothetically_newly_registered_tool_with_no_route_fires_the_gate() -> None:
    """Tier 2: non-vacuity -- simulates the actual #3464 scenario -- a brand
    new router=allow ToolDefinition name that was never wired anywhere --
    without registering a real ToolDefinition (which would require a full
    handler + schema and pollute the shared default registry). Confirms the
    gate does not silently accept an arbitrary unrecognised name."""
    allow_names = compute_router_allow_tool_names() | {"totally_new_tool_3464"}
    unreachable = compute_unreachable_router_allow_tool_names(allow_names=allow_names)
    assert "totally_new_tool_3464" in unreachable
    with pytest.raises(AssertionError):
        _assert_reachability_gate(unreachable)
