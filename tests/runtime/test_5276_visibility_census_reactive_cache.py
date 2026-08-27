"""Tier 2: #5276 (visibility_items) — the EXPENSIVE, envelope-only half of
``capability_visibility_state()`` (the tool/mcp/category/skill census
against the agent envelope) is memoized; only the CHEAP overlay
(``/visibility`` override + per-turn ephemeral taint) recomputes every call.

Root cause: ``capability_visibility_state()`` used to redo the ENTIRE
classification on every call, including every render frame — dominated by
``_reachable_tool_names``'s real, sync-bridged scheme ``build_presentation``
call (#3220) and a fresh ``resolved_profile_for`` read, both genuinely
non-trivial. Owner's own real-machine measurement, post-#5279 landing:
"visibility item 多発してそう" (this field looked dominant). Investigated
(not assumed): the ENVELOPE itself (topology/delegate/per-session config)
is grep-confirmed to be set at session construction/spawn/restore and never
mutated by any live in-session command, so memoizing the envelope-only
census causes no staleness there. What DOES change mid-session — and this
cache correctly tracks, invalidated synchronously (the #5279/#5284 lesson:
no EventLog subscriber) at ``_reapply_mcp``/``_reapply_skills`` — is WHICH
capabilities exist to classify (the MCP roster / skill registry).

Real ``AgentRegistry``/``Session`` (real envelope via
``spawn_session_recorded(narrowing=...)``) — no mocks, mirrors
``test_visibility_toggle_2285.py``'s own pattern. Uses the #4403 counting
technique on ``_reachable_tool_names`` (the expensive part) to witness
recompute counts.

Disclosed, not chased further (scope note): the envelope census's other
inputs via the active scheme's own ``build_presentation`` (e.g.
``list_available_agents()``, which could change on a sibling agent
spawn) are not independently grep-verified as invariant here — only the
two mutation sites Session itself owns (``_reapply_mcp``/
``_reapply_skills``) are covered. A future finding that some OTHER input
changes mid-session belongs in a follow-up issue, not silently assumed
away by this test suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _counting_wrapper(monkeypatch, cap_vis) -> dict:
    """Mirrors #4403's own counting technique — counts real
    ``_reachable_tool_names`` calls (the expensive scheme-census part of
    the envelope classification) from this point on."""
    real_fn = cap_vis._reachable_tool_names
    call_count = {"n": 0}

    def _counting(excluded_categories):
        call_count["n"] += 1
        return real_fn(excluded_categories)

    monkeypatch.setattr(cap_vis, "_reachable_tool_names", _counting)
    return call_count


@pytest.mark.asyncio
async def test_repeated_reads_cost_one_real_census(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — 3 repeated ``capability_visibility_state()``
    reads with no intervening mutation cost exactly 1 real census
    computation, not 3."""
    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    call_count = _counting_wrapper(monkeypatch, s._capability_visibility)

    r1 = s.capability_visibility_state()
    r2 = s.capability_visibility_state()
    r3 = s.capability_visibility_state()

    assert call_count["n"] == 1, (
        f"expected exactly 1 real census computation across 3 reads with "
        f"no intervening mutation, got {call_count['n']}"
    )
    assert r1 == r2 == r3


@pytest.mark.asyncio
async def test_a_visibility_toggle_does_not_trigger_a_recompute(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification contrast — a ``/visibility`` toggle
    (``set_capability_visible``) only mutates the CHEAP override
    (``hidden_by_session``), never the memoized envelope census, so it
    must not trigger a real recompute."""
    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    call_count = _counting_wrapper(monkeypatch, s._capability_visibility)

    before = s.capability_visibility_state()
    assert call_count["n"] == 1
    some_tool = before["authorized"][0]["name"] if before["authorized"] else None
    assert some_tool is not None, "test needs at least one authorized tool to toggle"

    s.set_capability_visible("tool", some_tool, False)
    after = s.capability_visibility_state()

    assert call_count["n"] == 1, (
        f"a /visibility toggle must not trigger a real census recompute "
        f"(it only affects the cheap override), got {call_count['n']} "
        f"real calls total"
    )
    assert any(
        row["kind"] == "tool" and row["name"] == some_tool
        for row in after["hidden_by_session"]
    ), "the toggle itself must still take effect in hidden_by_session"


@pytest.mark.asyncio
async def test_mcp_reload_invalidates_the_census(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — ``_reapply_mcp`` (the MCP-roster hot-reload
    seam) invalidates the census, so a subsequent read costs exactly 1
    more real computation."""
    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    call_count = _counting_wrapper(monkeypatch, s._capability_visibility)

    s.capability_visibility_state()
    assert call_count["n"] == 1

    await s._reapply_mcp({})
    s.capability_visibility_state()

    assert call_count["n"] == 2, (
        f"expected exactly 1 more real census computation after "
        f"_reapply_mcp, got {call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_categories_and_skills_are_never_ephemeral_checked(tmp_path, monkeypatch) -> None:
    """Tier 2: regression guard — a real bug caught mid-implementation of
    this PR: CATEGORY and SKILL rows must never be checked against the
    per-turn ephemeral gate (only TOOL/MCP rows were, in the pre-#5276
    code, and still are). Passing a maximally-restrictive
    ``ephemeral_contextual`` (denies everything) must still authorize
    every category/skill row exactly as with no ephemeral taint at all —
    only tool/mcp rows may move to ``denied_by_turn_context``."""
    from reyn.security.permissions.effective import ContextualPermission

    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    cap_vis = s._capability_visibility

    untainted = cap_vis.capability_visibility_state(ephemeral_contextual=None)
    deny_all = ContextualPermission(tool_allow=frozenset(), mcp_allow=frozenset())
    tainted = cap_vis.capability_visibility_state(ephemeral_contextual=deny_all)

    untainted_categories = {r["name"] for r in untainted["authorized"] if r["kind"] == "category"}
    tainted_categories = {r["name"] for r in tainted["authorized"] if r["kind"] == "category"}
    untainted_skills = {r["name"] for r in untainted["authorized"] if r["kind"] == "skill"}
    tainted_skills = {r["name"] for r in tainted["authorized"] if r["kind"] == "skill"}

    assert tainted_categories == untainted_categories, (
        "category rows must never be affected by the ephemeral gate"
    )
    assert tainted_skills == untainted_skills, (
        "skill rows must never be affected by the ephemeral gate"
    )
