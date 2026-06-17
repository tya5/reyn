"""Tier 2: OS invariant — AgentRegistry.rewind_to global consistent-cut rewind.

ADR-0038 Stage 1c-2 (D2). Real `AgentRegistry` + `StateLog` + on-disk agents
(no mocks). One global single-seq WAL + one workspace SSoT ⇒ a single
reset-record rewinds every agent atomically. Covers: active-target validation,
multi-agent consistent-cut reconstruct, the **self-contained** snapshot
(applied_seq = R) that makes restore_all correct-or-absent without a
floor-clamp (choice (b)), and the no-compaction-during-window guard.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import RewindIntoAbandonedError, rewind
from reyn.core.events.state_log import StateLog

_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


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


# ── two-substrate (workspace) coverage (ADR-0038 Stage 1d) ────────────────────


@_needs_git
@pytest.mark.asyncio
async def test_rewind_to_restores_workspace_to_active_gen(tmp_path):
    """Tier 2: rewind_to restores the workspace substrate to the as-of-N generation."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    ws = reg.workspace_store

    (tmp_path / "code.py").write_text("v1", encoding="utf-8")
    await _put(log, "alpha", "a1")          # seq 1
    await ws.capture(1)
    (tmp_path / "code.py").write_text("v2", encoding="utf-8")
    await _put(log, "alpha", "a2")          # seq 2
    await ws.capture(2)

    await reg.rewind_to(1)

    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "v1"   # workspace as-of-N


@_needs_git
@pytest.mark.asyncio
async def test_recover_rewind_restores_active_gen_not_abandoned(tmp_path):
    """Tier 2: crash mid-rewind ⇒ recovery brings BOTH substrates to as-of-N.

    Simulates a crash AFTER the reset-record is fsync'd but BEFORE materialisation
    (workspace still at the undone-future v2, no post-rewind capture). Recovery
    must restore the workspace to the ACTIVE gen-1 (v1) — NOT the abandoned gen-2
    tag (the highest raw tag <= head) — and re-materialise the runtime snapshot.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    ws = reg.workspace_store

    (tmp_path / "code.py").write_text("v1", encoding="utf-8")
    await _put(log, "alpha", "a1")          # seq 1  (active, gen-1 = v1)
    await ws.capture(1)
    (tmp_path / "code.py").write_text("v2", encoding="utf-8")
    await _put(log, "alpha", "a2")          # seq 2  (will be abandoned, gen-2 = v2)
    await ws.capture(2)

    # reset-record appended (rewind to 1), then crash BEFORE materialisation.
    R = await rewind(log, target_n=1)       # seq 3 — abandons (1, 3) incl. gen-2@2
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "v2"   # pre-recovery

    result = await reg.recover_rewind_if_needed()

    assert result is not None and result["recovered_target_n"] == 1
    # workspace: ACTIVE gen-1 (v1), NOT the abandoned gen-2 tag.
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "v1"
    # runtime: self-contained snapshot re-materialised as-of-N (applied_seq = head).
    snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(snap) == ["a1"]       # a2 (abandoned future) not present
    assert snap.applied_seq == R


@pytest.mark.asyncio
async def test_recover_rewind_is_noop_without_reset_record(tmp_path):
    """Tier 2: with no rewind record, recovery is a no-op (returns None)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    await _put(reg.state_log, "alpha", "a1")
    assert await reg.recover_rewind_if_needed() is None


@_needs_git
@pytest.mark.asyncio
async def test_restore_all_triggers_crash_recovery(tmp_path):
    """Tier 2: restore_all (production startup seam) TRIGGERS crash-mid-rewind recovery.

    Wiring proof — recovery must run via ``restore_all`` (the path the 3 startup
    sites call), NOT only via a direct ``recover_rewind_if_needed`` call. Simulate
    a crash mid-rewind (reset-record present, workspace still at the undone-future
    v2), then call ``restore_all``: the workspace must be restored to the ACTIVE
    gen-N (v1). Events target an unseeded agent so no non-empty snapshot pulls in
    the session factory.
    """
    reg = _make_registry(tmp_path)          # only the auto 'default' agent (empty)
    log = reg.state_log
    ws = reg.workspace_store

    (tmp_path / "code.py").write_text("v1", encoding="utf-8")
    await _put(log, "ghost", "g1")          # seq 1 (advances seq; 'ghost' not in list_names)
    await ws.capture(1)
    (tmp_path / "code.py").write_text("v2", encoding="utf-8")
    await _put(log, "ghost", "g2")          # seq 2
    await ws.capture(2)
    await rewind(log, target_n=1)           # seq 3 — abandons (1,3); crash before materialise
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "v2"   # pre-recovery

    await reg.restore_all()                 # production seam — must trigger recovery

    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "v1"   # recovered to active gen-N


# ── Stage 1e: retention clamp + GC + window guard ─────────────────────────────


class _WatermarkShim:
    """Minimal session exposing iter_applied_seqs (the floor-calc public surface)."""

    def __init__(self, seqs: list[int]) -> None:
        self._seqs = seqs

    def iter_applied_seqs(self, *, now_ts: float, long_await_threshold: float) -> list[int]:
        return list(self._seqs)


@pytest.mark.asyncio
async def test_compute_truncate_floor_clamped_by_retention(tmp_path):
    """Tier 2: a deeper retention policy clamps the truncate floor below the live floor."""
    from reyn.chat.registry import AgentRegistry
    from reyn.core.events.retention import RetentionPolicy

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
        retention_policy=RetentionPolicy(keep_generations=2),
    )
    _seed_agent(tmp_path, "alpha")
    reg._agents["alpha"] = _WatermarkShim([10])         # live floor = 11

    store = reg._store_for("alpha")                      # record 3 checkpoints
    for s in (1, 2, 3):
        snap = AgentSnapshot.empty("alpha")
        snap.applied_seq = s
        store.record(snap)

    # keep last 2 gens (2, 3) → oldest retained = 2 → floor = min(11, 2) = 2.
    assert reg.compute_truncate_floor() == 2


@pytest.mark.asyncio
async def test_live_policy_floor_unchanged(tmp_path):
    """Tier 2: default (live) policy applies NO clamp — floor stays the live floor."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )  # default policy = live
    _seed_agent(tmp_path, "alpha")
    reg._agents["alpha"] = _WatermarkShim([10])
    store = reg._store_for("alpha")
    for s in (1, 2, 3):
        snap = AgentSnapshot.empty("alpha")
        snap.applied_seq = s
        store.record(snap)

    assert reg.compute_truncate_floor() == 11            # min(watermark)+1, no clamp


@pytest.mark.asyncio
async def test_truncate_gcs_generations_below_floor(tmp_path):
    """Tier 2: generation GC drops gens below the floor (retained gens stay)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    store = reg._store_for("alpha")
    for s in (1, 2, 3):
        snap = AgentSnapshot.empty("alpha")
        snap.applied_seq = s
        store.record(snap)
    assert store.seqs() == [1, 2, 3]

    await reg._prune_generations_below(3)

    kept = reg._store_for("alpha").seqs()
    assert kept == [3]                                   # 1, 2 GC'd; 3 (>= floor) kept


@pytest.mark.asyncio
async def test_rewind_to_rejects_target_truncated_out_of_wal(tmp_path):
    """Tier 2: rewinding to a seq below the retained WAL raises (decision-enabling, Q4)."""
    from reyn.core.events.snapshot_generations import RewindBeyondRetentionError

    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a")          # seq 1
    await _put(log, "alpha", "b")          # seq 2
    await _put(log, "alpha", "c")          # seq 3
    await log.truncate_below(3)            # drop seq 1, 2; oldest kept = 3

    with pytest.raises(RewindBeyondRetentionError):
        await reg.rewind_to(2)             # seq 2 truncated → outside retention window
