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
census causes no staleness there — EXCEPT one real edge the census's own
``sid=self._session_id_provider()`` argument exposes: a session's own sid
CAN be re-keyed post-construction (spawn fixup — see
``capability_visibility.py``'s own module docstring), so a stale cache
survives-then-lies for the SAME session object across a re-key. Caught
during review (architect, #5285): invalidated at 3 sites, not 2 —
``_reapply_mcp``/``_reapply_skills`` (the MCP roster / skill registry, WHICH
capabilities exist to classify) AND ``load_persisted_toggles`` (called
after a re-key completes, per that method's own docstring), all
synchronously (the #5279/#5284 lesson: no ``EventLog`` subscriber).

Real ``AgentRegistry``/``Session`` (real envelope via
``spawn_session_recorded(narrowing=...)``) — no mocks, mirrors
``test_visibility_toggle_2285.py``'s own pattern. Uses the #4403 counting
technique on ``_reachable_tool_names`` (the expensive part) to witness
recompute counts.

Disclosed AND filed (architect, #5285 review — a docstring note alone
was judged insufficient for a panel-visible field): whether some OTHER
input to the active scheme's own ``build_presentation`` — e.g.
``list_available_agents()``, which could change on a sibling agent
spawn — changes mid-session independent of the 3 sites above is not
grep-verified here. Filed as #5288, same treatment as #5280.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.data.skills.registry import SkillEntry
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import CapabilityScope
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path, *, available_skills=None) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        capability_scope = (
            CapabilityScope(available_skills=available_skills)
            if available_skills is not None else None
        )
        s = make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            capability_scope=capability_scope,
        )
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
    only tool/mcp rows may move to ``denied_by_turn_context``.

    #5285 review (architect): the skill side must not compare empty-set
    to empty-set (six-questions ④ — that stays green no matter how the
    split is implemented). ``spawn_session_recorded`` already registers
    this repo's own real declared skills (confirmed non-empty below), so
    no fixture skill needs to be invented — asserted explicitly rather
    than assumed, so a future change that made this fixture skill-less
    would fail LOUDLY here instead of silently making the equality check
    vacuous."""
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

    assert untainted_skills, (
        "test fixture registered no skills — an empty set here would make "
        "the equality check below vacuous (six-questions ④)"
    )
    assert tainted_categories == untainted_categories, (
        "category rows must never be affected by the ephemeral gate"
    )
    assert tainted_skills == untainted_skills, (
        "skill rows must never be affected by the ephemeral gate"
    )


@pytest.mark.asyncio
async def test_skill_reload_invalidates_the_census(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — the 3rd site missing a witness (architect B on
    #5285): ``_reapply_skills`` (the skill-registry hot-reload seam) must
    invalidate the census, so a subsequent read costs exactly 1 more real
    computation. Mirrors ``test_mcp_reload_invalidates_the_census``."""
    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    call_count = _counting_wrapper(monkeypatch, s._capability_visibility)

    s.capability_visibility_state()
    assert call_count["n"] == 1

    # _reapply_skills re-reads the full config cascade for real (load_config)
    # but only ``build_skill_registry``'s OUTPUT needs to differ from the
    # empty starting set for the "did the roster change" branch to fall
    # through to the reassignment + invalidation this test targets.
    monkeypatch.setattr(
        "reyn.data.skills.registry.build_skill_registry",
        lambda skills_cfg: [SkillEntry(name="new-skill", description="d", path="skills/new/SKILL.md")],
    )
    await s._reapply_skills({})
    s.capability_visibility_state()

    assert call_count["n"] == 2, (
        f"expected exactly 1 more real census computation after "
        f"_reapply_skills, got {call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_load_persisted_toggles_invalidates_the_census(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — the 3rd invalidation site (architect B on
    #5285, finding ①): ``load_persisted_toggles`` is called right after a
    session re-key, and the census's own ``sid=self._session_id_provider()``
    argument means a re-key CAN change what it should return for the SAME
    session object. Calling ``load_persisted_toggles()`` must invalidate
    the cache, so a subsequent read costs exactly 1 more real computation."""
    reg = _make_registry(tmp_path)
    s = reg.get_session("alice", await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    ))
    call_count = _counting_wrapper(monkeypatch, s._capability_visibility)

    s.capability_visibility_state()
    assert call_count["n"] == 1

    s.load_persisted_toggles()
    s.capability_visibility_state()

    assert call_count["n"] == 2, (
        f"expected exactly 1 more real census computation after "
        f"load_persisted_toggles, got {call_count['n']} real calls total"
    )
