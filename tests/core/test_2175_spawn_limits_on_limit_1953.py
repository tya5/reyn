"""Tier 2: #2175 — spawn-limits routed through the safety.on_limit framework.

C3's max_depth/max_children no longer hard-reject in parallel; they route through the
SAME on_limit checkpoint as max_agent_hops (mode-driven: unattended=reject /
interactive=ask the operator → extend-on-approval / auto_extend). The base limit stays
operator-config-set (restart-only); the extension is human/operator-approved — the LLM
never self-raises. DEPTH / agent-fan-out / topology-size carry SEPARATE per-spawner
extension keys (approving one operation does not silently widen another).

Real AgentRegistry + StateLog + RouterHostAdapter + the real handle_limit_exceeded /
OnLimitConfig (no mocks; a real fixed-answer bus for the interactive path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import OnLimitConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from tests._support.router_host_adapter import make_adapter


def _registry(tmp_path: Path, *, max_depth: int = 0, max_children: int = 0) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log,
        max_spawn_depth=max_depth, max_spawn_children=max_children,
    )


# ── unattended = reject (the C3 hard-deny posture, now via the framework) ────────────


@pytest.mark.asyncio
async def test_depth_unattended_rejects(tmp_path):
    """Tier 2: on_limit=unattended → a spawn past max_depth rejects (the C3 limit+1
    falsification, now expressed through the on_limit framework, not a parallel path)."""
    reg = _registry(tmp_path, max_depth=2)
    await reg.create_agent("a0")
    await reg.create_agent("a1", parent="a0")
    await reg.create_agent("a2", parent="a1")  # at depth 2 == max
    adapter = make_adapter(agent_name="a2", agent_registry=reg,
                           on_limit=OnLimitConfig(mode="unattended"))
    res = await adapter.spawn_agent(name="a3", role="")  # depth 3 > 2
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"


# ── interactive = ask → approve → extend (the new posture) ──────────────────────────


@pytest.mark.asyncio
async def test_depth_interactive_approve_extends_and_proceeds(tmp_path):
    """Tier 2: (LOAD-BEARING) on_limit=interactive + operator approves → the over-limit
    spawn PROCEEDS, and the per-spawner extension is bumped so a re-spawn at that depth
    won't re-prompt. RED if the checkpoint isn't consulted / approval isn't honored."""
    reg = _registry(tmp_path, max_depth=2)
    await reg.create_agent("a0")
    await reg.create_agent("a1", parent="a0")
    await reg.create_agent("a2", parent="a1")  # depth 2 == max
    ext: dict = {}
    adapter = make_adapter(
        agent_name="a2", agent_registry=reg,
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0),
        safety_extensions=ext, intervention_answer="yes",
    )
    res = await adapter.spawn_agent(name="a3", role="")  # depth 3 > 2 → ask → approve
    assert res["status"] == "spawned"
    assert ext.get("max_spawn_depth:a2", 0) >= 1  # extension recorded (no re-prompt next)


@pytest.mark.asyncio
async def test_depth_interactive_decline_rejects(tmp_path):
    """Tier 2: on_limit=interactive + operator declines → the spawn is rejected (the
    decline path returns the spawn_limit_exceeded ack)."""
    reg = _registry(tmp_path, max_depth=2)
    await reg.create_agent("a0")
    await reg.create_agent("a1", parent="a0")
    await reg.create_agent("a2", parent="a1")
    adapter = make_adapter(
        agent_name="a2", agent_registry=reg,
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0),
        intervention_answer="no",
    )
    res = await adapter.spawn_agent(name="a3", role="")
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"


# ── auto_extend (the operator-opted budget) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_auto_extend_then_exhausts(tmp_path):
    """Tier 2: on_limit=auto_extend (times=1) → the over-limit child auto-extends once
    (spawned), the next exhausts the budget and rejects."""
    reg = _registry(tmp_path, max_children=2)
    await reg.create_agent("p")
    adapter = make_adapter(
        agent_name="p", agent_registry=reg,
        on_limit=OnLimitConfig(mode="auto_extend", auto_extend_times=1),
        safety_extensions={},
    )
    assert (await adapter.spawn_agent(name="c1", role=""))["status"] == "spawned"
    assert (await adapter.spawn_agent(name="c2", role=""))["status"] == "spawned"
    # 3rd hits the cap → auto_extend grants once
    assert (await adapter.spawn_agent(name="c3", role=""))["status"] == "spawned"
    # 4th → budget exhausted → reject
    res = await adapter.spawn_agent(name="c4", role="")
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"


# ── separate extension keys (Q3 approval-scoping) ───────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_and_topology_use_separate_extension_keys(tmp_path):
    """Tier 2: approving an agent-spawn FAN-OUT widen does NOT silently widen TOPOLOGY
    size — they key on separate extensions (max_spawn_fanout vs max_topology_members).
    RED if a single shared key let one approval leak into the other operation."""
    reg = _registry(tmp_path, max_children=2)
    await reg.create_agent("coord")
    await reg.create_agent("w1", parent="coord")
    await reg.create_agent("w2", parent="coord")
    ext: dict = {}
    # approve a 3rd direct child (fan-out extension)
    spawn_adapter = make_adapter(
        agent_name="coord", agent_registry=reg,
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0),
        safety_extensions=ext, intervention_answer="yes",
    )
    assert (await spawn_adapter.spawn_agent(name="w3", role=""))["status"] == "spawned"
    assert ext.get("max_spawn_fanout:coord", 0) >= 1
    # topology-members extension is UNTOUCHED by the fan-out approval
    assert ext.get("max_topology_members:coord", 0) == 0
    # a 4-member topology (> base 2) under UNATTENDED → still rejected (its own key)
    topo_adapter = make_adapter(
        agent_name="coord", agent_registry=reg,
        on_limit=OnLimitConfig(mode="unattended"), safety_extensions=ext,
    )
    res = await topo_adapter.create_topology(
        name="org", kind="network", members=["coord", "w1", "w2", "w3"],
    )
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"


# ── no-self-raise: base holds without an approval ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_approval_means_base_limit_holds(tmp_path):
    """Tier 2: with no checkpoint wired (the LLM path can't approve its own extension) the
    base operator limit holds — the over-limit spawn rejects. The extension only comes
    from the operator-approved checkpoint, never the LLM (no-self-raise)."""
    reg = _registry(tmp_path, max_depth=1)
    await reg.create_agent("a0")
    await reg.create_agent("a1", parent="a0")  # depth 1 == max
    adapter = make_adapter(agent_name="a1", agent_registry=reg)  # no on_limit → no checkpoint
    res = await adapter.spawn_agent(name="a2", role="")  # depth 2 > 1
    assert res["status"] == "error" and res["kind"] == "spawn_limit_exceeded"
