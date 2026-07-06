"""Tier 2: #2103 C3 — operator spawn-tree bounds (safety.spawn.max_depth/max_children).

A DoS guard on the LLM spawn primitives: an agent must not mint an unbounded spawn tree.
The bounds are OPERATOR-set in reyn.yaml (the restart-only OUT layer) — an LLM has no
runtime path to raise its own limit (a self-raisable limit is no limit). Enforced at the
LLM spawn SEAMS (host adapter): agent_spawn (depth + fan-out) and topology_create (org
size = fan-out). The operator CLI create path is unbounded (authority), consistent with
the C1 subtree forge-guard scope.

Real AgentRegistry + StateLog + RouterHostAdapter + the real config loader (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from tests._support.router_host_adapter import make_adapter


def _registry(tmp_path: Path, *, max_depth: int = 0, max_children: int = 0) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log,
        max_spawn_depth=max_depth, max_spawn_children=max_children,
    )


# ── the LOAD-BEARING no-self-raise gate ─────────────────────────────────────────────


def test_safety_spawn_is_restart_only_not_self_raisable():
    """Tier 2: (LOAD-BEARING) safety.spawn lives in reyn.yaml, which is NOT a hot-reload
    file — so the bound is restart-only OUT-set and an LLM cannot raise its own limit at
    runtime (a self-raisable limit is no limit). RED if reyn.yaml is ever added to the
    hot-reload set (= safety.* becomes runtime-mutable)."""
    from reyn.config.loader import _HOT_RELOAD_FILES
    assert "reyn.yaml" not in _HOT_RELOAD_FILES
    assert "reyn.local.yaml" not in _HOT_RELOAD_FILES
    # the hot-reload set is the narrow IN-layer (mcp/cron/hooks/skills/pipelines) —
    # none carry safety.*
    assert set(_HOT_RELOAD_FILES) == {
        "config/mcp.yaml", "config/cron.yaml", "config/hooks.yaml",
        "config/skills.yaml", "config/pipelines.yaml",
    }


def test_config_loader_parses_safety_spawn():
    """Tier 2: safety.spawn.{max_depth,max_children} round-trips through the real loader
    (defense-by-default non-zero defaults; operator override honoured)."""
    from reyn.config.chat import SafetyConfig, _build_safety_config
    assert SafetyConfig().spawn.max_depth > 0 and SafetyConfig().spawn.max_children > 0
    built = _build_safety_config({"spawn": {"max_depth": 3, "max_children": 4}})
    assert built.spawn.max_depth == 3 and built.spawn.max_children == 4


# ── max_depth enforcement (agent_spawn) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_rejected_beyond_max_depth(tmp_path):
    """Tier 2: (LOAD-BEARING) a spawn whose child would exceed max_depth is rejected at the
    host seam; a spawn AT the limit is allowed. RED if the depth bound isn't enforced."""
    reg = _registry(tmp_path, max_depth=2)
    await reg.create_agent("a0")                    # depth 0
    await reg.create_agent("a1", parent="a0")       # depth 1
    await reg.create_agent("a2", parent="a1")       # depth 2 (== max)

    # boundary: spawning under a1 (depth 1) → child depth 2 == max → ALLOWED
    res_ok = await make_adapter(agent_name="a1", agent_registry=reg).spawn_agent(
        name="a1b", role="")
    assert res_ok["status"] == "spawned"

    # spawning under a2 (depth 2) → child depth 3 > max → REJECTED
    res = await make_adapter(agent_name="a2", agent_registry=reg).spawn_agent(
        name="a3", role="")
    assert res["status"] == "error"
    assert res["kind"] == "spawn_limit_exceeded"


# ── max_children enforcement (agent_spawn fan-out) ──────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_rejected_beyond_max_children(tmp_path):
    """Tier 2: (LOAD-BEARING) a parent may spawn up to max_children direct children; the
    next is rejected. RED if the fan-out bound isn't enforced."""
    reg = _registry(tmp_path, max_children=2)
    await reg.create_agent("p")
    adapter = make_adapter(agent_name="p", agent_registry=reg)

    assert (await adapter.spawn_agent(name="c1", role=""))["status"] == "spawned"
    assert (await adapter.spawn_agent(name="c2", role=""))["status"] == "spawned"
    res = await adapter.spawn_agent(name="c3", role="")  # 3rd > max_children=2
    assert res["status"] == "error"
    assert res["kind"] == "spawn_limit_exceeded"


@pytest.mark.asyncio
async def test_fan_out_count_is_identity_keyed_not_name(tmp_path):
    """Tier 2: (the #2166 lens, carried forward by tui) the fan-out count keys on the
    parent's IDENTITY, not its name — an ORPHAN of a purged+reused parent does NOT count
    against the reused same-named parent's fan-out, so the reused parent gets its full
    budget (and is not charged for children it never spawned). RED if spawn_child_count
    counted by name (the orphan would consume a slot of the reused parent)."""
    reg = _registry(tmp_path, max_children=2)
    await reg.create_agent("par")                     # par identity #1
    await reg.create_agent("orphan", parent="par")    # edge orphan → (par, #1)
    assert reg.spawn_child_count("par") == 1

    await reg.archive_agent("par", purge=True)
    await reg.create_agent("par")                     # name reused → par identity #2

    # the orphan's edge froze identity #1 ≠ the reused par's #2 → not a child of the new par
    assert reg.spawn_child_count("par") == 0          # identity-keyed (a name count → 1)

    # so the reused par gets its FULL fan-out budget (not charged for the orphan)
    adapter = make_adapter(agent_name="par", agent_registry=reg)
    assert (await adapter.spawn_agent(name="n1", role=""))["status"] == "spawned"
    assert (await adapter.spawn_agent(name="n2", role=""))["status"] == "spawned"
    assert (await adapter.spawn_agent(name="n3", role=""))["status"] == "error"  # now at cap


# ── max_children enforcement (topology_create size) ─────────────────────────────────


@pytest.mark.asyncio
async def test_topology_create_rejected_beyond_max_children(tmp_path):
    """Tier 2: (LOAD-BEARING) max_children governs topology SIZE too — a topology with more
    members than max_children is rejected; at-limit is allowed. RED if the size bound isn't
    enforced. (Members are operator-created so the SIZE check, not the subtree guard,
    fires.)"""
    reg = _registry(tmp_path, max_children=2)
    await reg.create_agent("coord")
    await reg.create_agent("w1", parent="coord")
    await reg.create_agent("w2", parent="coord")
    await reg.create_agent("w3", parent="coord")
    adapter = make_adapter(agent_name="coord", agent_registry=reg)

    res = await adapter.create_topology(
        name="big", kind="network", members=["coord", "w1", "w2", "w3"],  # 4 > 2
    )
    assert res["status"] == "error"
    assert res["kind"] == "spawn_limit_exceeded"

    res_ok = await adapter.create_topology(
        name="small", kind="network", members=["coord", "w1"],  # 2 == max
    )
    assert res_ok["status"] == "created"


# ── operator-CLI scope: unbounded (authority) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_create_agent_is_unbounded(tmp_path):
    """Tier 2: (boundary control) the operator CLI create path (registry.create_agent
    directly, not the host seam) is UNBOUNDED — a deep chain past max_depth succeeds.
    The bound applies to the LLM seam only (authority, consistent with C1)."""
    reg = _registry(tmp_path, max_depth=1, max_children=1)
    await reg.create_agent("o0")
    await reg.create_agent("o1", parent="o0")
    await reg.create_agent("o2", parent="o1")  # depth 2 > max_depth=1, operator → allowed
    await reg.create_agent("o3", parent="o2")  # depth 3, still allowed
    assert reg.exists("o3")
    assert reg.spawn_depth("o3") == 3  # the chain was built unbounded
