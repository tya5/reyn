"""Tier 2: #2944 — the hoisted ``is_active`` predicate wired into
``AgentRegistry.restore_all``'s ``SubscriptionRegistry.replay`` call must produce
IDENTICAL restored bindings to the pre-hoist per-seq ``is_active_seq`` call shape,
across the rewind scenarios ADR-0038 Stage 1b-1e define: no rewind, one rewind,
nested/subsuming rewinds, and a checkout-back resurrection.

``build_active_predicate`` is already proven pointwise-equivalent to ``is_active_seq``
(``tests/test_build_active_predicate_equivalence_2941.py``); this test verifies the
WIRING at the actual #2944 call site — ``SubscriptionRegistry.replay`` — produces the
same live task→binding map under both call shapes, using real WAL
``task_subscribed``/``task_rebound`` entries (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import build_active_predicate, is_active_seq
from reyn.core.events.state_log import StateLog
from reyn.task.subscription import SubscriptionRegistry


def _snapshot(reg: SubscriptionRegistry) -> "dict[str, tuple[str | None, str | None, str]]":
    """Public-surface read of every binding (no private-state peek — ``exists`` /
    ``assignee_of`` / ``requester_of`` / ``requester_kind_of`` are all public)."""
    return {
        tid: (
            reg.assignee_of(tid), reg.requester_of(tid), reg.requester_kind_of(tid),
        )
        for tid in reg.task_ids()
    }


def _replay_both_ways(state_log: StateLog) -> None:
    """Replay via the OLD per-seq call shape and the NEW hoisted-predicate call
    shape into two fresh registries; assert their restored bindings match exactly."""
    old_way = SubscriptionRegistry()
    old_way.replay(
        state_log.iter_from(0), is_active=lambda s: is_active_seq(state_log, s),
    )
    new_way = SubscriptionRegistry()
    new_way.replay(
        state_log.iter_from(0), is_active=build_active_predicate(state_log),
    )
    assert _snapshot(old_way) == _snapshot(new_way), (
        "hoisted build_active_predicate wiring diverges from the per-seq "
        "is_active_seq call shape it replaced"
    )


async def _sub(log: StateLog, task_id: str, assignee: str) -> int:
    return await log.append(
        "task_subscribed", task_id=task_id, assignee=assignee,
        requester="alice", requester_kind="session",
    )


async def _rebind(log: StateLog, task_id: str, assignee: "str | None") -> int:
    return await log.append("task_rebound", task_id=task_id, assignee=assignee)


@pytest.mark.asyncio
async def test_equivalence_no_rewind(tmp_path: Path) -> None:
    """Tier 2: with no rewind, every subscription is active under both call shapes."""
    log = StateLog(tmp_path / "state.wal")
    await _sub(log, "t1", "alice")
    await _sub(log, "t2", "bob")
    await _rebind(log, "t1", "carol")
    _replay_both_ways(log)


@pytest.mark.asyncio
async def test_equivalence_one_rewind(tmp_path: Path) -> None:
    """Tier 2: one rewind abandons a binding — the abandoned interval must be
    hidden identically under both call shapes."""
    from reyn.core.events.snapshot_generations import checkout

    log = StateLog(tmp_path / "state.wal")
    await _sub(log, "t1", "alice")
    target = await _sub(log, "t2", "bob")          # rewind target
    await _rebind(log, "t2", "carol")               # abandoned by the rewind below
    await _sub(log, "t3", "dave")                    # abandoned too
    await checkout(log, target_seq=target)
    _replay_both_ways(log)


@pytest.mark.asyncio
async def test_equivalence_nested_subsuming_rewinds(tmp_path: Path) -> None:
    """Tier 2: a rewind, then more turns, then a rewind that subsumes the first —
    equivalence holds through the composition."""
    from reyn.core.events.snapshot_generations import checkout

    log = StateLog(tmp_path / "state.wal")
    await _sub(log, "t1", "alice")
    inner_target = await _sub(log, "t2", "bob")
    await _sub(log, "t3", "carol")
    await checkout(log, target_seq=inner_target)     # abandon t3's create (small tail)
    outer_target = await _sub(log, "t4", "dave")
    await _sub(log, "t5", "eve")
    await checkout(log, target_seq=outer_target)     # subsumes the first rewind entirely
    _replay_both_ways(log)


@pytest.mark.asyncio
async def test_equivalence_checkout_back_resurrection(tmp_path: Path) -> None:
    """Tier 2: rewind, branch forward, then checkout BACK to the old (now-abandoned)
    tip — resurrects it. Equivalence holds across the resurrection."""
    from reyn.core.events.snapshot_generations import checkout

    log = StateLog(tmp_path / "state.wal")
    await _sub(log, "t1", "alice")
    rewind_point = await _sub(log, "t2", "bob")
    tip = await _sub(log, "t3", "carol")              # abandoned by the rewind below
    await checkout(log, target_seq=rewind_point)      # rewind: abandons t3
    await _sub(log, "t4", "dave")                     # new branch forward
    await checkout(log, target_seq=tip)               # checkout back: resurrects t3
    _replay_both_ways(log)
