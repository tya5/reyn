"""Tier 2: OS invariant -- #5816, architect ruling on `AgentSnapshot.
applied_seq`'s own contract.

`applied_seq` is not an exact position and not an ownership claim -- it
is a LOWER BOUND: "every WAL entry belonging to this agent at seq <= P is
already absorbed." The two real writers (`SnapshotJournal`'s own
`last_assigned_seq` stamp, and `AgentSnapshot.apply_events`'s own "last
matching entry seen") both satisfy this same one invariant by picking
different, independently-safe values for P -- see the issue's own
architect-ruling comment for the full read-side census (`git grep -nE
"applied_seq" -- src/` finds zero readers that need "this agent's last
event" specifically).

**The dangerous direction** (architect, verbatim): a writer must never
claim a WAL position P as absorbed when an earlier, THIS-agent-owned
entry at a LOWER seq was never actually applied -- once a generation
recording that P is saved and the WAL truncates below it, that earlier
entry is gone forever, with no error. The safe direction (P too small)
only widens what gets re-replayed; harmless.

`AgentSnapshot.apply_events` itself trusts its own input completely --
it has no way to detect a GAP in the events it's handed (it only skips
`seq <= self.applied_seq`, it never checks "did I see every matching
seq back to my own applied_seq?"). The real protection today is
entirely in how the two real callers (`reconstruct` /
`AgentRegistry.restore_all`) BUILD that input: always starting the WAL
slice at `base.applied_seq + 1` (or the global `min(applied_seq) + 1`
for `restore_all`, safe because each snapshot's own `apply_events` still
filters `seq <= self.applied_seq` per-agent) -- gapless by construction.
This file pins THAT construction directly, on the real, reachable
`reconstruct()` call (the architect's own named check, #5816 ②):
nothing in this repo's test suite asserted it before this PR (measured:
`git grep -n "apply_events" -- tests/` found 0 tests exercising a
GAPPED delta).

Real `AgentRegistry`/`StateLog`/`SnapshotGenerationStore` throughout --
no mocks."""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, reconstruct
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


async def _put(log: StateLog, agent: str, msg_id: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=msg_id, msg_kind="user",
        payload={"text": msg_id},
    )


@pytest.mark.asyncio
async def test_reconstruct_replay_delta_starts_immediately_after_the_base_no_gap(
    tmp_path: Path,
) -> None:
    """Tier 2: #5816 -- the WAL entry immediately after a base generation's
    own `applied_seq` must be absorbed, not skipped. Two real WAL events
    for the SAME agent, both after the base: the NEARER one (at exactly
    `base.applied_seq + 1`) is the one a gapped delta would silently
    drop -- if it went missing, `applied_seq` would still advance past it
    (to the farther event's own seq), permanently hiding its loss."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    base_seq = await _put(log, "alpha", "base")
    store = reg._store_for("alpha")
    base_snap = AgentSnapshot.empty("alpha")
    base_snap.applied_seq = base_seq
    store.record(base_snap)

    near_seq = await _put(log, "alpha", "near")  # == base_seq + 1
    far_seq = await _put(log, "alpha", "far")

    snap = reconstruct("alpha", store, log, far_seq, scope=GLOBAL_SCOPE)

    ids = [m["id"] for m in snap.inbox]
    assert "near" in ids, (
        f"the WAL entry immediately after the base generation must be absorbed -- "
        f"got {ids!r} (a gap here silently, permanently loses it once the WAL "
        f"truncates below the resulting generation's own applied_seq)"
    )
    assert "far" in ids
    assert snap.applied_seq == far_seq


@pytest.mark.asyncio
async def test_apply_events_trusts_its_input_and_cannot_detect_a_withheld_own_event(
    tmp_path: Path,
) -> None:
    """Tier 2: #5816 -- names the hazard directly at the unit level (not
    just "the two real callers happen to avoid it"): `apply_events` has
    no mechanism of its own to refuse a gapped event list. Given ONLY a
    later matching event (the earlier one withheld, simulating a
    hypothetical future caller bug), it advances `applied_seq` to that
    later seq while the withheld event's own effect is genuinely,
    silently absent -- exactly the "claims P is absorbed when it isn't"
    shape the architect's ruling names as dangerous. This is not a
    defect in `apply_events` itself (its contract is "trust the caller",
    documented) -- it is the reason the OTHER test in this file, pinning
    the real callers' own gapless construction, is the actual guard."""
    snap = AgentSnapshot(agent_name="alpha")
    withheld = {
        "kind": "inbox_put", "seq": 1, "target": "alpha",
        "msg_id": "withheld", "msg_kind": "user", "payload": {},
    }
    seen = {
        "kind": "inbox_put", "seq": 4, "target": "alpha",
        "msg_id": "seen", "msg_kind": "user", "payload": {},
    }

    snap.apply_events([seen])  # `withheld` never passed -- the simulated gap

    assert snap.applied_seq == 4, (
        "apply_events advances to whatever seq it was actually given, with no "
        "way to know a lower, matching seq existed and was withheld"
    )
    assert not any(m["id"] == "withheld" for m in snap.inbox), (
        "the withheld event's own effect must be genuinely absent here -- this "
        "IS the hazard: applied_seq=4 now silently claims seq 1 is absorbed too"
    )


@pytest.mark.asyncio
async def test_truncate_falsify_applied_seq_lower_bound_survives_wal_truncation(
    tmp_path: Path,
) -> None:
    """Tier 2: CLAUDE.md hard rule (recovery-feature PRs need a
    truncate-falsify test) -- checked, and it DOES apply: this PR touches
    the recovery-core invariant `applied_seq` documents. Set X (a
    generation whose own `applied_seq` correctly reflects "near" already
    absorbed) -> truncate the WAL past X's own supporting events -> assert
    X survives (reconstructing again still shows "near", now sourced from
    the SAVED generation, not the truncated-away raw WAL entry)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    base_seq = await _put(log, "alpha", "base")
    store = reg._store_for("alpha")
    base_snap = AgentSnapshot.empty("alpha")
    base_snap.applied_seq = base_seq
    store.record(base_snap)

    near_seq = await _put(log, "alpha", "near")
    far_seq = await _put(log, "alpha", "far")

    # X: a real generation correctly absorbing both -- applied_seq=far_seq
    # is a real, accurate lower bound (both near and far are baked in).
    materialized = reconstruct("alpha", store, log, far_seq, scope=GLOBAL_SCOPE)
    store.record(materialized)

    # Truncate the WAL past far_seq's own supporting events -- near_seq's
    # raw WAL entry is now gone; only the saved generation remembers it.
    await log.truncate_below(far_seq + 1)
    await log.flush()

    reconstructed_after_truncation = reconstruct(
        "alpha", store, log, far_seq, scope=GLOBAL_SCOPE,
    )
    ids = [m["id"] for m in reconstructed_after_truncation.inbox]
    assert "near" in ids, (
        f"applied_seq's own lower-bound invariant must survive WAL truncation -- "
        f"got {ids!r}"
    )
    assert "far" in ids
    assert reconstructed_after_truncation.applied_seq == far_seq
