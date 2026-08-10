"""Tier 2: OS invariant — AgentRegistry.checkout(seq) unified time-travel primitive.

ADR-0038 D8 Phase-2 fork (#1533 2a-2). `checkout(seq)` generalises `rewind_to`:
it jumps the global consistent-cut to ANY seq — including one on an abandoned
(dead) branch (branch-switch / fork revival) — whereas Phase-1 `rewind_to` only
undoes to an active-branch seq. `rewind_to` is now the active-node special case
(a thin wrapper that keeps the `RewindIntoAbandonedError` guard, then delegates).

The load-bearing case is the full-path round-trip (abandoned → checkout →
continue → checkout-back) exercised through the REAL registry: reconstruct +
session re-adopt. It proves the elegant property verified at the interval layer —
because `reconstruct` / `_materialize_rewind` recompute `is_active` from the full
reset-record chain, a single guard-lifted reset-record expresses branch-switch
with no new persisted field, and the runtime substrate follows the *target's*
lineage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import RewindIntoAbandonedError, rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user", payload={"text": text},
    )


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


def _inbox_ids(tmp_path: Path, name: str) -> list[str]:
    return [m["id"] for m in AgentSnapshot.load(name, _snap_path(tmp_path, name)).inbox]


# ── load-bearing: full-path branch-switch round-trip (runtime substrate) ───────


@pytest.mark.asyncio
async def test_checkout_back_revives_lineage_runtime(tmp_path):
    """Tier 2: abandoned→checkout→continue→checkout-back drives the runtime substrate.

    The load-bearing N-way lineage case through the REAL registry (reconstruct +
    snapshot persist + on-disk state). Both checkout targets are on a DEAD branch
    (the lifted guard), and the inbox reconstructs along the TARGET's lineage each
    time — never the prior active one. Pure runtime (no git) so the lineage
    assertion stands without a workspace dependency.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    await _put(log, "alpha", "a1")          # seq 1
    await _put(log, "alpha", "a2")          # seq 2
    await reg.rewind_to(1)                   # seq 3 = R1 — undo, abandons {2}
    await _put(log, "alpha", "a3")          # seq 4 (active continuation)
    assert _inbox_ids(tmp_path, "alpha") == ["a1"]   # post-rewind active = [a1] (a3 not yet materialised on disk)

    # checkout to seq 2 (ABANDONED — a2's dead branch): revives a2, abandons a3.
    res2 = await reg.checkout(2)
    assert res2["target_n"] == 2
    assert _inbox_ids(tmp_path, "alpha") == ["a1", "a2"]   # target lineage, NOT [a1,a3]
    # self-contained snapshot pinned to the new reset-record (same invariant as rewind).
    assert AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha")).applied_seq == res2["reset_seq"]

    # checkout BACK to seq 4 (now abandoned — a3's lineage): revives a3, re-abandons a2.
    res4 = await reg.checkout(4)
    assert res4["target_n"] == 4
    assert _inbox_ids(tmp_path, "alpha") == ["a1", "a3"]   # lineage swapped back, no a2 leakage


# ── rewind_to = active-node special case (equivalence + preserved guard) ───────


@pytest.mark.asyncio
async def test_checkout_to_active_seq_equals_rewind_to(tmp_path):
    """Tier 2: checkout(active_seq) == rewind_to(active_seq) (the undo special case).

    With no abandoned branch, checkout to an active seq is exactly an undo — same
    consistent-cut, same self-contained snapshot, same surviving inbox.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")          # seq 1
    await _put(log, "alpha", "a2")          # seq 2

    res = await reg.checkout(1)               # active target → undo semantics
    assert res["target_n"] == 1
    assert _inbox_ids(tmp_path, "alpha") == ["a1"]   # a2 abandoned, as rewind_to(1) would
    assert AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha")).applied_seq == res["reset_seq"]


@pytest.mark.asyncio
async def test_rewind_to_still_rejects_abandoned_after_refactor(tmp_path):
    """Tier 2: rewind_to wrapper preserves the Phase-1 abandoned-target guard.

    Refactoring rewind_to into a thin wrapper over checkout must NOT drop its
    guard — Phase-1 undo still rejects an abandoned target (only the unified
    checkout lifts it). Regression lock on the wrapper.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a")           # seq 1
    await _put(log, "alpha", "b")           # seq 2
    await rewind(log, target_n=1)            # seq 3 — abandons seq 2

    with pytest.raises(RewindIntoAbandonedError):
        await reg.rewind_to(2)               # abandoned target — undo still rejects

    # but checkout to the same abandoned seq is ALLOWED (the lifted guard).
    res = await reg.checkout(2)
    assert res["target_n"] == 2
    assert _inbox_ids(tmp_path, "alpha") == ["a", "b"]   # a2 lineage revived
