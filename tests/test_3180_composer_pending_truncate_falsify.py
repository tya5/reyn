"""Tier 2: #3180 — the recovery-feature PR gate for ``DurablePendingStore``.

CLAUDE.md requires any PR adding recovery / reconstruction functionality to
carry a truncate-falsify witness in the SAME PR: set a pending X, truncate the
WAL below X's source events, reconstruct, assert X survives. ``deadline``'s
armed state is the sharpest instance of the rule's motivation — WAL-derived
recovery state that is not snapshot-backed is a silent data-loss vector, and a
dead-man switch that silently fails to re-arm reports healthy while watching
nothing.

Real ``StateLog`` / ``DurablePendingStore`` / ``Composer`` / ``HookBus``
throughout (CLAUDE.md mock ban) — the WAL is a genuine on-disk file that is
genuinely rewritten by ``truncate_below``.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.bus import HookBus
from reyn.hooks.composer import (
    Composer,
    ComposerDef,
    ComposerInput,
    ComposerOp,
    ComposerPolicy,
)
from reyn.hooks.durable_pending_store import STORE_FILENAME, DurablePendingStore
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern


def _input(kind: str) -> ComposerInput:
    return ComposerInput(kind=kind, pattern=EventPattern(kind=kind, payload=None))


def _deadline_def() -> ComposerDef:
    return ComposerDef(
        name="job_overdue", op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind="composed:job_overdue", correlate_by="job_id",
        policy=ComposerPolicy(ttl_seconds=1800.0),
        durable=True,
    )


@pytest.mark.asyncio
async def test_armed_deadline_survives_wal_truncation_below_its_source_events(tmp_path):
    """Tier 2: an armed ``deadline`` survives a WAL truncation that drops every
    event of the run that armed it, and comes back with its ORIGINAL arm
    instant — so the missed-deadline fire lands at ``armed_at + ttl``, not at
    ``restart_time + ttl``.

    The arm instant is the load-bearing half. A reconstruction that restored
    "j1 is being watched" but restarted the clock (the shape a boot-time re-arm
    from a job registry produces) would pass a presence-only assertion while
    silently sliding the deadline forward by the entire crash window — exactly
    the interval the dead-man switch exists to notice.

    RED if the store were WAL-event-derived: the arm's source events are
    asserted GONE from the raw WAL file post-truncation before reconstruction
    is attempted, so a replay-based store has nothing left to rebuild from.
    """
    wal = tmp_path / "state" / "wal.jsonl"
    store_path = tmp_path / "state" / STORE_FILENAME
    state_log = StateLog(wal)
    bus = HookBus()
    definition = _deadline_def()

    # ── Pre-crash: arm the deadline, with real WAL activity around it ───────
    await state_log.append("inbox_put", marker="pre-arm")
    store = DurablePendingStore(store_path)
    composer = Composer(definition, bus=bus, pending_store=store)
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    armed_at = store.get("job_overdue", "j1").created_at
    arm_seq = state_log.current_seq
    await state_log.append("inbox_consume", marker="post-arm")
    await state_log.flush()

    pre_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert any('"pre-arm"' in ln for ln in pre_lines), (
        "sanity: the run's WAL events must be durable before we truncate them"
    )

    # ── Truncation: push the floor PAST every event of the arming run ───────
    for i in range(150):
        await state_log.append("inbox_put", n=i)
    await state_log.truncate_below(state_log.current_seq - 5)
    await state_log.flush()
    assert state_log.last_truncate_stats["dropped"] >= 1
    post_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert not any('"pre-arm"' in ln or '"post-arm"' in ln for ln in post_lines), (
        "the arming run's source events must actually be GONE from the WAL "
        "(not merely counted as dropped) — otherwise a WAL-derived store would "
        "pass this test vacuously"
    )
    assert state_log.current_seq > arm_seq
    await state_log.aclose()  # the crash: tear the WAL worker down

    # ── Reconstruct: a fresh store + fresh Composer, same paths ─────────────
    recovered_store = DurablePendingStore(store_path)
    recovered = Composer(definition, bus=bus, pending_store=recovered_store)
    restored = recovered_store.get("job_overdue", "j1")
    assert restored is not None, (
        "the armed deadline must survive WAL truncation below its own run's "
        "events — the store is a full-state snapshot file, never WAL-derived"
    )
    assert restored.created_at == armed_at, (
        "the ORIGINAL arm instant must survive: a reset clock silently extends "
        "the deadline by the whole crash window"
    )

    # ── And it still fires on the original schedule ─────────────────────────
    sub = bus.subscribe()
    recovered.sweep(now=armed_at + 1799)
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()  # not yet due — ttl is measured from the ORIGINAL arm
    recovered.sweep(now=armed_at + 1801)
    fired = sub.get_nowait()
    assert fired.kind == "composed:job_overdue"
    assert fired.payload["correlation_key"] == "j1"
    assert fired.payload["armed_at"] == armed_at


@pytest.mark.asyncio
async def test_disarm_before_crash_survives_too(tmp_path):
    """Tier 2: the falsifying direction of the same gate — a deadline DISARMED
    before the crash must stay disarmed after reconstruction.

    Durability that only ever restores arms would resurrect a completed job's
    monitor and fire a false missed-deadline on every restart. Both edges
    (``put`` on arm, ``delete`` on disarm) must be durable, so this pins the
    ``delete`` leg the arm-only test cannot see.
    """
    wal = tmp_path / "state" / "wal.jsonl"
    store_path = tmp_path / "state" / STORE_FILENAME
    state_log = StateLog(wal)
    bus = HookBus()
    definition = _deadline_def()

    store = DurablePendingStore(store_path)
    composer = Composer(definition, bus=bus, pending_store=store)
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    armed_at = store.get("job_overdue", "j1").created_at
    composer.handle_event(HookEvent(kind="orch:job_done", payload={"job_id": "j1"}))

    for i in range(150):
        await state_log.append("inbox_put", n=i)
    await state_log.truncate_below(state_log.current_seq - 5)
    await state_log.flush()
    await state_log.aclose()

    recovered = Composer(definition, bus=bus, pending_store=DurablePendingStore(store_path))
    sub = bus.subscribe()
    recovered.sweep(now=armed_at + 100000)
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()  # the disarm was durable too — no false missed-deadline
