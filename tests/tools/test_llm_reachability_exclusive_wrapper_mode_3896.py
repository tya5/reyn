"""Tier 2: exclusive-wrapper-mode LLM-reachability gate (#3896).

#3896 found that ``router_tools.build_tools``'s section J strip step
(``_wrapper_superseded_tool_names()``, run whenever
``universal_wrappers_enabled=True`` — the exclusive-wrapper mode #3429
landed) removed ``spawn_session`` from direct advertisement with no
compensating catalog route — a genuine capability loss under a real,
selectable, already-landed configuration. The plain #3464 gate
(``reyn.tools.llm_reachability.compute_unreachable_router_allow_tool_names``)
cannot see this: its route (a) census answers "reachable under SOME valid
parameter combination" (deliberately existential — several tools are
correctly conditional), and ``spawn_session`` IS reachable under the OTHER
combination (wrappers off), so it never enters that gate's unreachable set
at all. This suite exercises the SECOND, mode-scoped computation
(``compute_*_under_exclusive_wrapper_mode``) that models the strip step for
real (imports ``router_tools._wrapper_superseded_tool_names()``, never
re-derives it) and answers the mode-scoped question directly.

**Fixed** (owner ruling, 2026-08-13, option 1): ``spawn_session`` gained a
``multi_agent`` catalog route, so it is reachable again under this mode via
route (b) even though §J still strips route (a) — same shape as
``call_mcp_tool``/``describe_mcp_tool`` below. It no longer appears in
``UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS``.

Per lead-coder's explicit review instruction (still true for the tools that
remain in the registry): the RED this modeling produces for a genuinely
unreachable tool is not suppressed via xfail/skip — it is registered in
``UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS``, a closed, tracked
registry mirroring #3464's own pattern, so growth of this set is caught the
same way #3464's own gate catches growth of its set.

No mocks — everything here reads the real default ``ToolRegistry`` /
``KNOWN_ACTION_NAMES`` / ``build_tools`` source / ``router_tools``'s own
strip set, or passes a plain string/set override to the module's own
testability seams.
"""
from __future__ import annotations

import inspect

import pytest

from reyn.tools.llm_reachability import (
    UNREACHABLE_CLASSIFICATIONS,
    UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS,
    compute_direct_advertisable_tool_names,
    compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode,
    compute_reachable_tool_names_under_exclusive_wrapper_mode,
    compute_router_allow_tool_names,
    compute_unreachable_router_allow_tool_names,
    compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode,
)


def _assert_exclusive_wrapper_mode_gate(unreachable: frozenset[str]) -> None:
    """Same identity-check shape as #3464's own gate, scoped to this mode's
    registry — factored out so both the real-state test and the
    strip-falsify tests exercise the identical assertion."""
    declared = frozenset(UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS)
    undeclared = unreachable - declared
    assert not undeclared, (
        f"router=allow tool(s) {sorted(undeclared)} are not reachable under "
        f"exclusive-wrapper mode (stripped direct advertisement NOR "
        f"invoke_action) and are not declared in "
        f"UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS -- register them "
        f"with a reason (see src/reyn/tools/llm_reachability.py), or wire "
        f"them into reachability under this mode if that was an oversight "
        f"(#3896)."
    )
    stale = declared - unreachable
    assert not stale, (
        f"UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS entries "
        f"{sorted(stale)} are declared unreachable under this mode but the "
        f"census now finds them reachable -- remove the stale entry (its "
        f"wiring fix should ship WITH the removal so the regression stays "
        f"covered, not silently)."
    )


def test_current_unreachable_set_under_exclusive_wrapper_mode_matches_the_declared_registry() -> None:
    """Tier 2: registry allow-set minus (stripped-direct union invoke_action)
    equals exactly UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS' keys."""
    unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode()
    _assert_exclusive_wrapper_mode_gate(unreachable)


def test_declared_reasons_use_the_closed_classification_and_are_nonempty() -> None:
    """Tier 2: no entry may register a hole without a real classification +
    a reason with actual content."""
    assert UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS, (
        "the registry should not be empty while #3896/#3464 are open"
    )
    for name, entry in UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS.items():
        assert entry.classification in UNREACHABLE_CLASSIFICATIONS, (
            f"{name!r} has classification {entry.classification!r}, not one "
            f"of {sorted(UNREACHABLE_CLASSIFICATIONS)}"
        )
        assert isinstance(entry.reason, str) and len(entry.reason.strip()) >= 40, (
            f"{name!r}'s reason is missing or too short to be a real "
            f"falsifiable explanation: {entry.reason!r}"
        )


def test_spawn_session_is_reachable_under_exclusive_wrapper_mode() -> None:
    """Tier 2: #3896's fix (owner ruling, option 1) — spawn_session gained a
    `multi_agent` catalog route, so it is reachable under exclusive-wrapper
    mode via route (b) even though §J still strips its direct-advertisement
    route (a). No longer declared in this registry at all (was
    PENDING_CAPABILITY_DECISION before the fix)."""
    assert "spawn_session" not in UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS
    reachable = compute_reachable_tool_names_under_exclusive_wrapper_mode()
    assert "spawn_session" in reachable
    # The route making it reachable is (b), not (a): §J still strips its
    # direct-advertisement route (now correctly — it's a catalog member, so
    # advertising it BOTH directly and via invoke_action would be the same
    # #3429 double-spelling problem this whole strip step exists to avoid),
    # same shape as call_mcp_tool/describe_mcp_tool above. Confirms this
    # isn't accidentally passing because the strip itself regressed.
    from reyn.runtime.router_tools import _wrapper_superseded_tool_names
    assert "spawn_session" in _wrapper_superseded_tool_names()
    assert "spawn_session" not in compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode()
    # Sanity: spawn_session IS also in the #3464 gate's reachable set
    # (existentially reachable under wrappers=off) -- confirming this
    # mode-scoped registry answers a genuinely different question, not a
    # subset of #3464's own.
    assert "spawn_session" not in compute_unreachable_router_allow_tool_names()


def test_call_mcp_tool_and_describe_mcp_tool_are_declared_superseded_not_pending() -> None:
    """Tier 2: unlike spawn_session, these two ARE intentionally superseded
    by a differently-named catalog equivalent (router_tools.py's own
    _WRAPPER_SUPERSEDED_BASE_TOOLS comments name the replacement for each)
    -- the LLM's capability is unchanged, so nothing is pending an owner
    decision. Distinguishing this from PENDING_CAPABILITY_DECISION is the
    whole point of the SUPERSEDED_BY_CATALOG_REPLACEMENT classification."""
    for name in ("call_mcp_tool", "describe_mcp_tool"):
        assert name in UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS
        entry = UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS[name]
        assert entry.classification == "SUPERSEDED_BY_CATALOG_REPLACEMENT"


def test_cron_tools_are_declared_pending_capability_decision_here_too() -> None:
    """Tier 2: cron's unreachability is mode-independent (never advertised
    directly, never a catalog action, regardless of universal_wrappers_enabled)
    -- it belongs in BOTH registries, not just #3464's."""
    cron_names = {"cron_register", "cron_unregister", "cron_list", "cron_enable", "cron_disable"}
    assert cron_names <= set(UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS)
    for name in cron_names:
        assert (
            UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS[name].classification
            == "PENDING_CAPABILITY_DECISION"
        )


# ── Non-vacuity: strip-falsify each route, AND the strip-modeling itself ──
#
# #3896's own regression witness (`test_unmodeled_strip_would_hide_spawn_
# session_the_actual_3896_defect`) lived here until spawn_session gained a
# catalog route: its premise -- that spawn_session is unreachable under this
# mode -- is no longer true (it's reachable via route (b) regardless of
# whether the strip step is modeled), so the assertion it made would either
# invert or become vacuous. Deleted rather than inverted: the two probes
# below already independently prove both routes are load-bearing for THIS
# mode generally (create_topology for route (a), search_knowledge for route
# (b)), so no coverage is lost by retiring the spawn_session-specific one.


def test_strip_direct_advertisement_route_makes_the_gate_fire() -> None:
    """Tier 2: non-vacuity -- removing a tool ONLY reachable via the
    stripped route (a) (never routed through invoke_action, and not itself
    in the strip set) from the AST census must produce an undeclared
    unreachable tool under this mode too, mirroring #3464's own probe.

    ``create_topology`` is confirmed to not be a catalog action and not in
    the strip set, so it stays reachable under exclusive-wrapper mode via
    route (a) alone -- stripping its lookup call site removes it from both
    routes' union under this mode."""
    from reyn.runtime.router_tools import _wrapper_superseded_tool_names, build_tools

    superseded = _wrapper_superseded_tool_names()
    assert "create_topology" not in superseded, (
        "create_topology must not be in the strip set for this probe to be valid"
    )

    real_source = inspect.getsource(build_tools)
    anchor = '_registry.lookup("create_topology")'
    assert real_source.count(anchor) == 1, (
        "strip anchor must be unique or this falsifies the wrong call site"
    )
    stripped_source = real_source.replace(anchor, '_registry.lookup("__stripped_3896__")')

    baseline_unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode()
    assert "create_topology" not in baseline_unreachable

    stripped_unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode(
        source_text=stripped_source,
    )
    assert "create_topology" in stripped_unreachable

    with pytest.raises(AssertionError):
        _assert_exclusive_wrapper_mode_gate(stripped_unreachable)


def test_strip_invoke_action_route_makes_the_gate_fire() -> None:
    """Tier 2: non-vacuity -- removing a tool ONLY reachable via route (b)
    must equally produce an undeclared unreachable tool under this mode,
    proving route (b) is load-bearing here too, not decorative.

    ``search_knowledge`` is confirmed to have no direct ``lookup`` call
    site -- invoke-route-only, so stripping it from route (b) removes it
    from the union entirely regardless of route (a)'s strip modeling."""
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

    direct_reachable = compute_direct_advertisable_tool_names()
    assert "search_knowledge" not in direct_reachable, (
        "search_knowledge must be invoke-route-only for this strip to be a valid probe"
    )

    baseline_unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode()
    assert "search_knowledge" not in baseline_unreachable

    stripped_action_names = frozenset(KNOWN_ACTION_NAMES - {"search_knowledge"})
    stripped_unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode(
        action_names=stripped_action_names,
    )
    assert "search_knowledge" in stripped_unreachable

    with pytest.raises(AssertionError):
        _assert_exclusive_wrapper_mode_gate(stripped_unreachable)


def test_a_hypothetically_newly_registered_tool_with_no_route_fires_the_gate() -> None:
    """Tier 2: non-vacuity -- a brand new router=allow ToolDefinition name
    that was never wired anywhere is caught under this mode too."""
    allow_names = compute_router_allow_tool_names() | {"totally_new_tool_3896"}
    unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode(
        allow_names=allow_names,
    )
    assert "totally_new_tool_3896" in unreachable
    with pytest.raises(AssertionError):
        _assert_exclusive_wrapper_mode_gate(unreachable)


def test_direct_advertisable_under_exclusive_wrapper_mode_is_a_subset_of_the_plain_census() -> None:
    """Tier 2: the stripped route (a) can only ever be a SUBSET of the plain
    (existential) census -- stripping removes names, never adds any. A
    regression that made the mode-scoped set LARGER than the plain one would
    mean the strip modeling itself is wrong (adding names from nowhere)."""
    plain = compute_direct_advertisable_tool_names()
    stripped = compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode()
    assert stripped <= plain
    assert stripped != plain, (
        "sanity: the real strip set is non-empty, so the stripped census "
        "must differ from the plain one on live main"
    )


def test_reachable_under_exclusive_wrapper_mode_matches_the_union_helper() -> None:
    """Tier 2: the convenience union function is exactly what the
    identity-check test manually re-derives via allow - reachable -- keeps
    the two from silently diverging if one is edited without the other."""
    reachable = compute_reachable_tool_names_under_exclusive_wrapper_mode()
    unreachable = compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode()
    allow = compute_router_allow_tool_names()
    assert unreachable == (allow - reachable)
