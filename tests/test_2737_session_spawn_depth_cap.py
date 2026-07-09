"""Tier 2: #2737 — session_spawn NESTING depth cap at PARITY with agent_spawn.

``max_spawn_depth`` (``safety.spawn.max_depth``) gated the ``agent_spawn`` path only —
the LLM ``session_spawn`` path was uncapped, so a spawned child's router host re-exposes
``spawn_session`` and grandchildren/great-grandchildren nest without limit (#2708
P3-item3 co-vet edge). Two consequences the cap bounds by construction:
  1. resource — unbounded session nesting;
  2. recursion — the compositional ``SpawnBridgeInterventionListener.bus()`` walk (#2735)
     recurses once per nesting level to resolve ask_user toward the root operator, so a
     deep chain risks a ``RecursionError``. The nesting depth this cap counts is EXACTLY
     that walk's length (both walk the same ``SpawnBridge*`` parent chain), so a capped
     depth ⇒ a bounded ``bus()`` recursion.

The fix reuses the SAME operator base cap + on_limit checkpoint + typed
``spawn_limit_exceeded`` error as ``agent_spawn`` (uniform sibling), enforced at the
``router_host_adapter.spawn_session`` seam.

Real ``AgentRegistry`` + real ``Session`` (factory forwards the spawn bridges) + real
``RouterHostAdapter`` + real on_limit framework — no collaborator mocks. Assertions use
the public spawn surface (adapter ack) + the public ``session_nesting_depth`` read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import OnLimitConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_buses import SpawnBridgeInterventionListener
from reyn.runtime.spawn_routing import BridgeToParent
from tests._support.router_host_adapter import make_adapter


def _registry(tmp_path: Path, state_log: StateLog, *, max_depth: int = 0) -> AgentRegistry:
    """Real AgentRegistry whose Session factory forwards BOTH spawn overrides (the
    widened factory protocol), so a spawned child carries the parent-bound
    ``SpawnBridgeInterventionListener`` bridge the nesting-depth walk counts."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
        max_spawn_depth=max_depth,
    )
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


async def _nest_once(reg: AgentRegistry, parent: Session) -> str:
    """Spawn one child SESSION under ``worker`` bridged to ``parent`` via the raw recorded
    seam (bypasses the cap — used only to BUILD a chain the cap is then tested against)."""
    routing = BridgeToParent(parent)
    return await reg.spawn_session_recorded(
        "worker",
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )


def _teardown_tasks(reg: AgentRegistry) -> None:
    for task in list(getattr(reg, "_tasks", {}).values()):
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_session_nesting_depth_counts_the_bridge_chain(tmp_path: Path) -> None:
    """Tier 2: ``session_nesting_depth`` returns the LLM-spawn nesting depth — main = 0,
    each ``session_spawn`` edge +1 — by walking the ``SpawnBridgeInterventionListener``
    parent chain. This is the SAME chain ``bus()`` recurses over, so this quantity is the
    recursion depth the cap bounds. (Grounds the parity + recursion-bound claim.)"""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    main = reg.get_or_load("worker")
    assert reg.session_nesting_depth("worker") == 0  # a root/main session

    child_sid = await _nest_once(reg, main)
    assert reg.session_nesting_depth("worker", child_sid) == 1

    child = reg.get_session("worker", child_sid)
    gc_sid = await _nest_once(reg, child)
    assert reg.session_nesting_depth("worker", gc_sid) == 2

    # The counted chain IS the bus() recursion chain: each level carries a
    # SpawnBridgeInterventionListener whose bus() resolves toward the root in bounded hops.
    gc = reg.get_session("worker", gc_sid)
    assert isinstance(gc.intervention_bridge, SpawnBridgeInterventionListener)
    assert gc.intervention_bridge.bus() is not None  # terminates (bounded recursion)


@pytest.mark.asyncio
async def test_session_spawn_beyond_cap_is_rejected(tmp_path: Path) -> None:
    """Tier 2: (RED on main = session_spawn is uncapped) a ``session_spawn`` that would
    nest past ``max_spawn_depth`` is rejected with the SAME typed ``spawn_limit_exceeded``
    error ``agent_spawn`` returns. cp-falsify: removing the adapter cap → this spawns
    instead of rejecting (RED). unattended on_limit = the C3 hard-deny posture."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log, max_depth=1)
    main = reg.get_or_load("worker")
    child_sid = await _nest_once(reg, main)  # nesting depth 1 == max

    adapter = make_adapter(
        agent_name="worker", agent_registry=reg, session_id=child_sid,
        on_limit=OnLimitConfig(mode="unattended"),
    )
    try:
        res = await adapter.spawn_session(
            request="do a thing", mode="persistent", narrowing=None, chain_id="c1",
        )
    finally:
        _teardown_tasks(reg)
    # PARITY: same error shape as agent_spawn's spawn-limit reject (status + kind).
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"


@pytest.mark.asyncio
async def test_session_spawn_within_cap_still_works(tmp_path: Path) -> None:
    """Tier 2: the bound is at PARITY, NOT stricter — a ``session_spawn`` that stays WITHIN
    ``max_spawn_depth`` proceeds and spawns. Guards against an off-by-one that would reject
    a legal nesting depth."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log, max_depth=2)
    main = reg.get_or_load("worker")
    child_sid = await _nest_once(reg, main)  # nesting depth 1; cap 2 ⇒ one more allowed

    adapter = make_adapter(
        agent_name="worker", agent_registry=reg, session_id=child_sid,
        on_limit=OnLimitConfig(mode="unattended"),
    )
    try:
        res = await adapter.spawn_session(
            request="do a thing", mode="persistent", narrowing=None, chain_id="c1",
        )
    finally:
        _teardown_tasks(reg)
    assert res["status"] == "spawned"


@pytest.mark.asyncio
async def test_session_spawn_interactive_approve_extends_and_proceeds(
    tmp_path: Path,
) -> None:
    """Tier 2: PARITY with agent_spawn's interactive path — on_limit=interactive + operator
    approves → the over-cap ``session_spawn`` PROCEEDS, and its SEPARATE per-agent extension
    key is bumped (session nesting ≠ agent-tree depth: approving one must not silently widen
    the other, the #2175 approval-scoping principle)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log, max_depth=1)
    main = reg.get_or_load("worker")
    child_sid = await _nest_once(reg, main)  # depth 1 == max

    ext: dict = {}
    adapter = make_adapter(
        agent_name="worker", agent_registry=reg, session_id=child_sid,
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0),
        safety_extensions=ext, intervention_answer="yes",
    )
    try:
        res = await adapter.spawn_session(
            request="do a thing", mode="persistent", narrowing=None, chain_id="c1",
        )
    finally:
        _teardown_tasks(reg)
    assert res["status"] == "spawned"
    # SEPARATE key from agent_spawn's max_spawn_depth:<agent> (approval-scoping).
    assert ext.get("max_session_depth:worker", 0) >= 1
    assert ext.get("max_spawn_depth:worker", 0) == 0
