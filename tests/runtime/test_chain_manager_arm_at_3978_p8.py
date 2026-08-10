"""Tier 2: OS invariant — ChainManager arm_at persistence (proposal 0067 P8,
#3978, "ttl expiry: reuse the chain-timeout shape, plus persist arm_at").

Pre-P8 bug this closes: ``restore()`` called ``arm_timeout()`` exactly like
a brand-new arm — a full ``chain_timeout_seconds`` window from the moment of
restore, regardless of how much of that window had already elapsed before
the crash. A crash near a chain's deadline silently EXTENDED the effective
deadline by up to a full window, every restart. P8 persists ``arm_at`` (the
absolute wall-clock deadline) so ``restore()`` can schedule against
whatever's LEFT of it instead.

Real ``ChainManager``/``SnapshotJournal``/``StateLog``/``AgentSnapshot``
throughout — no mocks, matching ``test_chain_manager_settle_3978.py``'s and
``test_chain_manager_find_chain.py``'s established pattern. The injected
``clock_fn`` computes ``arm_at``/``remaining`` deterministically (no real
sleep needed to test THAT arithmetic); the watchdog's actual scheduling
still runs on real ``asyncio.sleep``, so tests that need to observe it FIRE
use a short real duration + a bounded ``asyncio.wait_for`` on an event it
sets — never a fixed sleep as the thing that makes the assertion pass
(CLAUDE.md's testing policy).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.task_types import Requester


class _NullEvents:
    def emit(self, *_args, **_kwargs) -> None:
        pass


def _make_manager(
    tmp_path: Path, *, chain_timeout_seconds: float, clock_fn=None,
) -> "tuple[ChainManager, SnapshotJournal]":
    """Returns (manager, journal) — the journal is a real collaborator the
    TEST constructs and owns (not the manager's private state), so a test
    can assert on ``journal.snapshot.pending_chains`` (a real public
    property) to verify the WAL/snapshot mirror side, distinct from
    ``manager.get(chain_id)``'s in-memory read."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=chain_timeout_seconds, max_hop_depth=10,
        clock_fn=clock_fn,
    )
    return mgr, journal


async def _noop_fire(_chain_id: str) -> None:
    pass


# ── arm_timeout() persists arm_at ────────────────────────────────────────


@pytest.mark.asyncio
async def test_arm_timeout_persists_arm_at_on_the_chain_and_the_snapshot(
    tmp_path: Path,
):
    """Tier 2: arm_timeout() computes arm_at = clock() + chain_timeout_seconds
    and persists it both on the in-memory _PendingChain AND the journal's
    snapshot (the field register()'s own fields dict never carried — it's
    computed at arm time, not registration time)."""
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mgr, journal = _make_manager(
        tmp_path, chain_timeout_seconds=30, clock_fn=lambda: fixed_now,
    )
    await mgr.register(chain_id="c1", depth=0, original_text="q", sender=None)
    await mgr.arm_timeout("c1", on_fire=_noop_fire)

    expected = fixed_now + timedelta(seconds=30)
    assert mgr.get("c1").arm_at == expected
    assert journal.snapshot.pending_chains["c1"]["arm_at"] == expected.isoformat()


@pytest.mark.asyncio
async def test_arm_timeout_disabled_does_not_set_arm_at(tmp_path: Path):
    """Tier 2: non-vacuity — chain_timeout_seconds<=0 (timeouts disabled) is
    the existing arm_timeout() no-op; arm_at must stay unset, not a
    deadline nothing will ever check."""
    mgr, journal = _make_manager(tmp_path, chain_timeout_seconds=0)
    await mgr.register(chain_id="c1", depth=0, original_text="q", sender=None)
    await mgr.arm_timeout("c1", on_fire=_noop_fire)
    assert mgr.get("c1").arm_at is None
    assert "arm_at" not in journal.snapshot.pending_chains["c1"]


# ── restore() schedules against the REMAINING deadline, not a fresh window ──


@pytest.mark.asyncio
async def test_restore_of_a_past_due_chain_fires_promptly_not_after_a_fresh_window(
    tmp_path: Path,
):
    """Tier 2: the P8 bug, falsified directly. A chain whose persisted
    arm_at is already in the PAST (crash happened after the real deadline)
    must fire on its very next scheduler tick after restore — NOT get a
    fresh chain_timeout_seconds window. chain_timeout_seconds is set LARGE
    (60s) specifically so a bounded wait proves this: if restore() were
    still doing the pre-P8 "arm exactly like new", on_fire would not fire
    within this test's short bounded wait at all."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    past_arm_at = now - timedelta(seconds=5)  # 5s overdue
    snapshot = AgentSnapshot(
        agent_name="alpha",
        pending_chains={
            "c-overdue": {
                "chain_id": "c-overdue",
                "requester": {"agent_name": "worker", "session_id": "main"},
                "origin_depth": 0,
                "original_request": "q",
                "waiting_on": [],
                "arm_at": past_arm_at.isoformat(),
            },
        },
    )
    journal.install(snapshot)
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=60, max_hop_depth=10, clock_fn=lambda: now,
    )

    fired = asyncio.Event()

    async def _on_fire(chain_id: str) -> None:
        assert chain_id == "c-overdue"
        fired.set()

    mgr.restore(on_fire=_on_fire)

    await asyncio.wait_for(fired.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_restore_of_a_chain_with_time_remaining_waits_the_remainder_not_the_full_window(
    tmp_path: Path,
):
    """Tier 2: falsification pair — a chain that still has a SHORT real
    remainder (0.05s of a 60s window) fires within that short remainder.
    chain_timeout_seconds is 60s so this genuinely distinguishes "scheduled
    for the remainder" from "scheduled for the full window" — if restore()
    regressed to the pre-P8 fresh-arm behavior, this bounded wait would
    time out."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    soon_arm_at = now + timedelta(seconds=0.05)
    snapshot = AgentSnapshot(
        agent_name="alpha",
        pending_chains={
            "c-soon": {
                "chain_id": "c-soon",
                "requester": {"agent_name": "worker", "session_id": "main"},
                "origin_depth": 0,
                "original_request": "q",
                "waiting_on": [],
                "arm_at": soon_arm_at.isoformat(),
            },
        },
    )
    journal.install(snapshot)
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=60, max_hop_depth=10, clock_fn=lambda: now,
    )

    fired = asyncio.Event()

    async def _on_fire(chain_id: str) -> None:
        fired.set()

    mgr.restore(on_fire=_on_fire)

    await asyncio.wait_for(fired.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_restore_without_a_persisted_arm_at_falls_back_to_a_fresh_full_window(
    tmp_path: Path,
):
    """Tier 2: non-vacuity — a LEGACY chain (pre-P8 WAL entry, no arm_at
    key at all) restores exactly like before P8: a fresh full-window arm.
    Proven by NOT firing within a short bounded wait, with
    chain_timeout_seconds set to something real callers would use (not 60s
    — a short-but-not-instant value keeps the test itself fast while still
    genuinely distinguishing "fresh full window" from "already fired")."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    snapshot = AgentSnapshot(
        agent_name="alpha",
        pending_chains={
            "c-legacy": {
                "chain_id": "c-legacy",
                "requester": {"agent_name": "worker", "session_id": "main"},
                "origin_depth": 0,
                "original_request": "q",
                "waiting_on": [],
                # No "arm_at" key — the pre-P8 shape.
            },
        },
    )
    journal.install(snapshot)
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=0.3, max_hop_depth=10,
    )

    fired = asyncio.Event()

    async def _on_fire(chain_id: str) -> None:
        fired.set()

    mgr.restore(on_fire=_on_fire)

    assert mgr.get("c-legacy").arm_at is None, (
        "a legacy restore must not fabricate a deadline that was never recorded"
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(fired.wait(), timeout=0.05)
    # ...but it DOES eventually fire, on the full fresh window — proves the
    # watchdog was actually scheduled, not silently dropped.
    await asyncio.wait_for(fired.wait(), timeout=2.0)


# ── restore() stays sync (no cascading get_or_load()/restore_state() to async) ──


def test_restore_is_a_sync_method(tmp_path: Path):
    """Tier 1: Contract — restore() is deliberately NOT async (see its own
    docstring): Session.restore_state() is reached from
    AgentRegistry.get_or_load(), a sync method with its own wide sync
    caller graph outside P8's scope. A future edit that makes restore()
    async without addressing that graph should fail here, loud, rather
    than surface as an unawaited-coroutine RuntimeWarning somewhere deep
    in registry.py."""
    import inspect

    assert not inspect.iscoroutinefunction(ChainManager.restore)


# ── recovery gate: arm_at survives WAL truncation, end to end ──────────────


@pytest.mark.asyncio
async def test_truncate_falsify_restore_schedules_from_the_snapshot_backed_arm_at(
    tmp_path: Path,
):
    """Tier 2c: CLAUDE.md's recovery-feature PR gate. The field-level
    survival of ``arm_at`` through WAL truncation is already covered
    generically by ``test_agent_snapshot.py::
    test_truncate_falsify_chain_update_field_survives_wal_truncation``
    (#4110 — literally used ``arm_at=42.0`` as its own example field,
    anticipating this PR). What THIS PR adds beyond that is a real
    CONSUMER of the recovered field — ``ChainManager.restore()`` reading
    it and scheduling the remaining time — so this test closes the loop
    end to end: real ``chain_register``+``chain_update`` WAL events →
    baked into a SAVED snapshot (``applied_seq`` past both) → reload and
    replay an EMPTY tail (simulating truncation below both source
    events) → hand that reconstructed snapshot to a REAL ChainManager via
    ``journal.install()`` → ``restore()`` schedules the watchdog from the
    snapshot-recovered ``arm_at``, not a replayed one (there is nothing
    left to replay) and not a fresh window.
    """
    from reyn.core.events.agent_snapshot import AgentSnapshot as _Snap

    def _event(kind: str, seq: int, **fields) -> dict:
        return {"kind": kind, "seq": seq, "target": "alpha", **fields}

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    soon_arm_at = (now + timedelta(seconds=0.05)).isoformat()
    snap = _Snap.empty("alpha")
    snap.apply_events([
        _event(
            "chain_register", 1, chain_id="c-recover",
            requester={"agent_name": "worker", "session_id": "main"},
            origin_depth=0, original_request="x",
        ),
        _event("chain_update", 2, chain_id="c-recover", arm_at=soon_arm_at),
    ])
    assert snap.applied_seq == 2
    assert snap.pending_chains["c-recover"]["arm_at"] == soon_arm_at

    # Serialize → the snapshot carries arm_at + applied_seq=2.
    snap.save(tmp_path / "snap-recover.json")

    # TRUNCATE: reload and replay an EMPTY tail — all 3 source WAL events
    # are gone below the truncation floor.
    reloaded = _Snap.load("alpha", tmp_path / "snap-recover.json")
    reloaded.apply_events([])
    assert reloaded.pending_chains["c-recover"]["arm_at"] == soon_arm_at, (
        "arm_at must survive WAL truncation via the snapshot, not replay"
    )

    # Hand the TRUNCATED reconstruction to a real ChainManager and restore.
    log = StateLog(tmp_path / "wal2.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap-live.json", state_log=log,
    )
    journal.install(reloaded)
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=60, max_hop_depth=10, clock_fn=lambda: now,
    )
    fired = asyncio.Event()

    async def _on_fire(chain_id: str) -> None:
        assert chain_id == "c-recover"
        fired.set()

    mgr.restore(on_fire=_on_fire)
    # Fires from the ~0.05s REMAINING on the recovered deadline — if
    # restore() had regressed to a fresh 60s window (the pre-P8 bug this
    # gate exists to catch), this bounded wait would time out.
    await asyncio.wait_for(fired.wait(), timeout=2.0)

    # WAL-only CONTROL: a DIFFERENT chain, registered+updated entirely
    # AFTER the snapshot's applied_seq, is WAL-only — present when its
    # events are replayed, LOST when truncated instead. Proves the
    # assertion above is snapshot-backed survival, not something that
    # always passes regardless of truncation.
    later_events = [
        _event(
            "chain_register", 4, chain_id="c-walonly",
            requester={"agent_name": "worker", "session_id": "main"},
            origin_depth=0, original_request="y",
        ),
        _event("chain_update", 5, chain_id="c-walonly", arm_at=soon_arm_at),
    ]
    replayed = _Snap.load("alpha", tmp_path / "snap-recover.json")
    replayed.apply_events(later_events)
    assert replayed.pending_chains["c-walonly"]["arm_at"] == soon_arm_at
    truncated = _Snap.load("alpha", tmp_path / "snap-recover.json")
    truncated.apply_events([])  # seq 4/5 events truncated
    assert "c-walonly" not in truncated.pending_chains  # WAL-only state LOST


# ── lead-coder's suggestion: an async-producer (run_prompt_async) chain ────


@pytest.mark.asyncio
async def test_arm_at_persists_and_restores_for_an_async_producer_registered_chain(
    tmp_path: Path,
):
    """Tier 2: P4e's run_prompt(collect="async") producer registers a chain
    with kind="prompt" and |waiting_on|==1 (a single-waiter task, per
    architect's cardinality ruling — distinct shape from the legacy
    delegate-relay chains the tests above use, kind=None). arm_at
    persistence/restore is generic over the chain's kind — this proves it,
    rather than assuming the delegate-relay coverage above generalizes."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mgr = ChainManager(
        journal=journal, events=_NullEvents(),
        chain_timeout_seconds=30, max_hop_depth=10, clock_fn=lambda: fixed_now,
    )
    await mgr.register(
        chain_id="prompt-1", depth=1, original_text="do the thing",
        sender="caller_agent",
        waiting_on={"target_agent"},
        requester=Requester(agent_name="caller_agent", session_id="caller_sid"),
        origin_depth=1,
        kind="prompt",
    )
    await mgr.arm_timeout("prompt-1", on_fire=_noop_fire)

    expected = fixed_now + timedelta(seconds=30)
    assert mgr.get("prompt-1").arm_at == expected
    assert mgr.get("prompt-1").kind == "prompt"
    assert mgr.get("prompt-1").waiting_on == {"target_agent"}

    # Restore from the persisted snapshot — the async-producer shape
    # (kind="prompt", single waiter) must round-trip through arm_at
    # recovery exactly like the delegate-relay shape does.
    journal2 = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap2.json", state_log=log,
    )
    journal2.install(journal.snapshot)
    mgr2 = ChainManager(
        journal=journal2, events=_NullEvents(),
        chain_timeout_seconds=30, max_hop_depth=10,
        clock_fn=lambda: fixed_now + timedelta(seconds=10),  # 10s later
    )
    mgr2.restore(on_fire=_noop_fire)
    restored = mgr2.get("prompt-1")
    assert restored.arm_at == expected
    assert restored.kind == "prompt"
