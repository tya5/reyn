"""Tier 2: OS invariant — AgentRegistry.rewind_to global consistent-cut rewind.

ADR-0038 Stage 1c-2 (D2). Real `AgentRegistry` + `StateLog` + on-disk agents
(no mocks). One global single-seq WAL + one workspace SSoT ⇒ a single
reset-record rewinds every agent atomically. Covers: active-target validation,
multi-agent consistent-cut reconstruct, the **self-contained** snapshot
(applied_seq = R) that makes restore_all correct-or-absent without a
floor-clamp (choice (b)), and the no-compaction-during-window guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.snapshot_generations import RewindIntoAbandonedError, rewind
from reyn.events.state_log import StateLog


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    """Create the on-disk profile so list_names() includes the agent."""
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


def _inbox_ids(snap: AgentSnapshot) -> list[str]:
    return [m["id"] for m in snap.inbox]


@pytest.mark.asyncio
async def test_rewind_to_rejects_abandoned_target(tmp_path):
    """Tier 2: rewinding into an abandoned segment is rejected up front (1b guard)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a")          # seq 1
    await _put(log, "alpha", "b")          # seq 2
    await rewind(log, target_n=1)          # seq 3 — abandons seq 2

    with pytest.raises(RewindIntoAbandonedError):
        await reg.rewind_to(2)             # seq 2 is on an abandoned branch


@pytest.mark.asyncio
async def test_rewind_to_reconstructs_all_agents_as_of_n(tmp_path):
    """Tier 2: rewind_to reconstructs EVERY agent as-of-N (global consistent-cut).

    Two agents with interleaved appends; rewind_to(2) cuts the whole world back
    to seq 2 — each agent's on-disk snapshot reflects only its <=2 work, and is
    pinned to applied_seq = R (the reset-record) so it is self-contained.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    await _put(log, "alpha", "a1")         # seq 1 (<=2, kept)
    await _put(log, "beta", "b1")          # seq 2 (<=2, kept)
    await _put(log, "alpha", "a2")         # seq 3 (>2, abandoned)
    await _put(log, "beta", "b2")          # seq 4 (>2, abandoned)

    result = await reg.rewind_to(2)
    R = result["reset_seq"]

    assert result["target_n"] == 2
    # consistent-cut covers EVERY known agent (incl. the auto-created default).
    assert {"alpha", "beta"} <= set(result["agents"])

    alpha = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    beta = AgentSnapshot.load("beta", _snap_path(tmp_path, "beta"))
    assert _inbox_ids(alpha) == ["a1"]     # a2 abandoned
    assert _inbox_ids(beta) == ["b1"]      # b2 abandoned
    # self-contained: replay floor is R, not N.
    assert alpha.applied_seq == R
    assert beta.applied_seq == R


@pytest.mark.asyncio
async def test_rewind_to_snapshot_self_contained_for_restore_all(tmp_path):
    """Tier 2: the persisted snapshot makes restore_all correct WITHOUT is_active.

    restore_all replays forward from snapshot.applied_seq (no is_active honoring).
    Because rewind_to pins applied_seq = R, restore_all's replay starts past the
    abandoned (N, R] segment — so post-rewind work is kept and the abandoned
    future is never resurrected, even though restore_all doesn't know about
    rewind records. This is the correctness basis for choice (b).
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")         # seq 1 (kept)
    await _put(log, "alpha", "a2")         # seq 2 (abandoned by rewind to 1)

    result = await reg.rewind_to(1)
    R = result["reset_seq"]

    # New active-branch work after the rewind.
    await _put(log, "alpha", "a3")         # seq R+1

    # Simulate restore_all's algorithm: load the self-contained snapshot, then
    # replay forward from applied_seq + 1 (NO is_active honoring).
    saved = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert saved.applied_seq == R
    saved.apply_events(list(log.iter_from(saved.applied_seq + 1)))

    assert _inbox_ids(saved) == ["a1", "a3"]   # a2 (abandoned, <R) never replayed

    # Idempotent: replaying again from a fresh load yields the same state.
    again = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    again.apply_events(list(log.iter_from(again.applied_seq + 1)))
    assert _inbox_ids(again) == ["a1", "a3"]


@pytest.mark.asyncio
async def test_rewind_to_gates_compaction_during_window(tmp_path):
    """Tier 2: while a rewind is in progress, maybe_truncate_for_size no-ops.

    The guard returns None BEFORE the size/floor logic — proven by passing a
    1-byte threshold (the WAL exceeds it, so a non-guarded call would proceed
    past the threshold check) and still getting None while the flag is set.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    for i in range(5):
        await _put(log, "alpha", f"m{i}")

    reg._rewind_in_progress = True
    result = await reg.maybe_truncate_for_size(threshold_bytes=1)
    assert result is None                  # gated — no compaction during rewind


@pytest.mark.asyncio
async def test_rewind_to_drives_loaded_session_to_as_of_n_zero_residue(tmp_path):
    """Tier 2: rewind_to drives a REAL loaded session through the full path.

    A real ChatSession (aligned to the registry's on-disk paths) is injected; the
    user-facing rewind cancels + quiesces it, reconstructs as-of-N, clears live
    in-memory residue (reset_for_rewind), and re-adopts the as-of-N snapshot
    (restore_state). Post-rewind the session reflects as-of-N with zero pre-rewind
    residue — and its on-disk snapshot is the self-contained recovery artifact.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    session = ChatSession(
        agent_name="alpha", state_log=log,
        snapshot_path=_snap_path(tmp_path, "alpha"),
    )
    session.register_intervention_listener("test")
    reg._agents["alpha"] = session

    await _put(log, "alpha", "a1")         # seq 1 (kept by rewind to 1)
    await _put(log, "alpha", "a2")         # seq 2 (abandoned)
    # live in-memory residue that ONLY reset_for_rewind can clear (no WAL append):
    session.inbox.put_nowait(("user", {"text": "OLD-residue"}))

    result = await reg.rewind_to(1)

    # the live session reflects as-of-N (a1), OLD residue gone.
    drained = []
    while not session.inbox.empty():
        drained.append(session.inbox.get_nowait())
    assert drained == [("user", {"text": "a1"})]

    # on-disk snapshot for the loaded agent is as-of-N + self-contained (applied_seq=R).
    snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(snap) == ["a1"]
    assert snap.applied_seq == result["reset_seq"]
