"""Tier 2: OS invariant — retention policy + truncation floor clamp (ADR-0038 1e).

Real `StateLog` + `SnapshotGenerationStore` + `reconstruct` (no mocks). The clamp
guarantees the compaction floor never rises past what `reconstruct` needs for any
retained checkpoint — the concrete form of the 1c-1 `maybe_truncate` caveat. The
load-bearing test is the **reconstructability invariant**: every checkpoint inside
the retention window is still reconstructable after a clamped truncate, including
across a rewind (abandoned-segment) boundary.
"""
from __future__ import annotations

import pytest

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.retention import RetentionPolicy, compute_retention_floor
from reyn.events.snapshot_generations import (
    SnapshotGenerationStore,
    reconstruct,
    rewind,
)
from reyn.events.state_log import StateLog

AGENT = "alpha"


async def _put(log: StateLog, text: str) -> int:
    return await log.append(
        "inbox_put", target=AGENT, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _ids(snap: AgentSnapshot) -> list[str]:
    return [m["id"] for m in snap.inbox]


# ── RetentionPolicy ──────────────────────────────────────────────────────────


def test_default_policy_is_live():
    """Tier 2: default policy = live (no deeper retention); any axis set = not live."""
    assert RetentionPolicy().is_live is True
    assert RetentionPolicy(keep_generations=3).is_live is False
    assert RetentionPolicy(keep_duration_secs=10.0).is_live is False


def test_from_config():
    """Tier 2: from_config(None/{}) = live; a config block sets the axes."""
    assert RetentionPolicy.from_config(None).is_live is True
    assert RetentionPolicy.from_config({}).is_live is True
    p = RetentionPolicy.from_config({"keep_generations": 5})
    assert p.keep_generations == 5 and p.is_live is False


# ── compute_retention_floor ──────────────────────────────────────────────────


def test_live_policy_returns_live_floor_unchanged():
    """Tier 2: live policy applies no clamp — backward compatible."""
    assert compute_retention_floor(
        RetentionPolicy(), live_floor=42, checkpoint_seqs=[10, 20, 30],
    ) == 42


def test_keep_generations_clamps_down_to_oldest_retained():
    """Tier 2: keep last N checkpoints → floor clamps to the N-th most recent."""
    # gens at 10/20/30, keep last 2 → oldest retained = 20 → floor min(99, 20) = 20.
    assert compute_retention_floor(
        RetentionPolicy(keep_generations=2), live_floor=99,
        checkpoint_seqs=[10, 20, 30],
    ) == 20


def test_fewer_generations_than_window_keeps_all():
    """Tier 2: with fewer checkpoints than N, the oldest is retained."""
    assert compute_retention_floor(
        RetentionPolicy(keep_generations=5), live_floor=99,
        checkpoint_seqs=[10, 20],
    ) == 10


def test_no_generations_falls_back_to_live_floor():
    """Tier 2: with no checkpoints recorded, the floor stays at the live floor."""
    assert compute_retention_floor(
        RetentionPolicy(keep_generations=2), live_floor=42, checkpoint_seqs=[],
    ) == 42


def test_clamp_never_exceeds_live_floor():
    """Tier 2: floor is min(live_floor, retained) — never keeps less than live."""
    # retained gen 30 > live_floor 5 → floor stays at live 5 (don't truncate live state).
    assert compute_retention_floor(
        RetentionPolicy(keep_generations=1), live_floor=5,
        checkpoint_seqs=[30],
    ) == 5


# ── reconstructability invariant (the load-bearing test) ──────────────────────


@pytest.mark.asyncio
async def test_retained_checkpoints_reconstructable_after_clamped_truncate(tmp_path):
    """Tier 2: every retained checkpoint reconstructs after a clamped truncate.

    Build 3 checkpoints; keep last 2; truncate the WAL at the clamped floor; then
    each retained checkpoint must still reconstruct correctly even though older WAL
    entries are gone (the generation base bakes them in).
    """
    log = StateLog(tmp_path / "wal")
    store = SnapshotGenerationStore(AGENT, tmp_path / "gens")

    await _put(log, "a")                                   # seq 1
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @1
    await _put(log, "b")                                   # seq 2
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @2
    await _put(log, "c")                                   # seq 3
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @3

    floor = compute_retention_floor(
        RetentionPolicy(keep_generations=2), live_floor=log.current_seq + 1,
        checkpoint_seqs=store.seqs(),
    )
    assert floor == 2                                       # keep gens @2, @3
    await log.truncate_below(floor)                         # drops seq 1

    # retained checkpoints still reconstruct correctly (gen base bakes truncated history)
    assert _ids(reconstruct(AGENT, store, log, 2)) == ["a", "b"]
    assert _ids(reconstruct(AGENT, store, log, 3)) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_retained_checkpoints_reconstructable_across_rewind(tmp_path):
    """Tier 2: the clamp keeps rewind records that affect retained points (is_active).

    A rewind abandons a segment; the rewind record sits ABOVE the retained floor
    (its seq > any retained abandoned seq), so the clamped truncate keeps it and
    is_active stays derivable — the retained checkpoint reconstructs on the active
    branch.
    """
    log = StateLog(tmp_path / "wal")
    store = SnapshotGenerationStore(AGENT, tmp_path / "gens")

    await _put(log, "a")                                   # seq 1
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @1
    await _put(log, "b")                                   # seq 2 (will be abandoned)
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @2
    await rewind(log, target_n=1)                          # seq 3 — abandons (1,3) incl b
    await _put(log, "d")                                   # seq 4 (active, new branch)
    store.record(reconstruct(AGENT, store, log, log.current_seq))  # gen @4

    floor = compute_retention_floor(
        RetentionPolicy(keep_generations=2), live_floor=log.current_seq + 1,
        checkpoint_seqs=store.seqs(),                       # [1, 2, 4]
    )
    # keep last 2 checkpoints (2, 4); the rewind record @3 sits within [floor, head]
    assert floor == 2
    await log.truncate_below(floor)

    # active-branch reconstruct at head: a (kept) + d (post-rewind), b abandoned.
    assert _ids(reconstruct(AGENT, store, log, log.current_seq)) == ["a", "d"]
