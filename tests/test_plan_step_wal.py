"""Tier 2: plan_step_* events promoted to WAL (ADR-0023 Phase 2 step 4).

The resume analyzer (Step 7) needs deterministic step pairing across
restart, so plan_step_started / plan_step_completed / plan_step_failed
move from forensic-only events log to WAL. Phase 1's existing emits in
``execute_plan`` will be re-routed in Step 5 (PlanRuntime).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.state_log import WAL_EVENT_KINDS, StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal


def _make_journal(tmp_path: Path) -> tuple[SnapshotJournal, StateLog]:
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="default",
        snapshot_path=tmp_path / "snapshot.json",
        state_log=log,
    )
    return journal, log


# ── WAL_EVENT_KINDS registration ──────────────────────────────────────────


def test_plan_step_kinds_registered_in_wal_event_kinds() -> None:
    """Tier 2: plan_step_started/completed/failed are in WAL_EVENT_KINDS so
    StateLog.append accepts them and replay vocabulary covers them."""
    assert "plan_step_started" in WAL_EVENT_KINDS
    assert "plan_step_completed" in WAL_EVENT_KINDS
    assert "plan_step_failed" in WAL_EVENT_KINDS


def test_state_log_rejects_unknown_kinds(tmp_path: Path) -> None:
    """Tier 2: typos surface immediately at write time (= ADR-0001
    invariant — WAL vocabulary is closed)."""
    log = StateLog(tmp_path / "wal.jsonl")
    import asyncio

    async def call() -> None:
        with pytest.raises(ValueError, match="unknown WAL event kind"):
            await log.append("plan_step_starts")  # typo

    asyncio.run(call())


# ── SnapshotJournal recording ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_plan_step_started_appends_wal_with_full_payload(
    tmp_path: Path,
) -> None:
    """Tier 2: record_plan_step_started writes a WAL entry with the
    documented field set and returns the assigned seq."""
    journal, log = _make_journal(tmp_path)
    seq = await journal.record_plan_step_started(
        plan_id="p001", step_id="s1",
        depends_on=["s0"], n_tools=2,
    )
    assert isinstance(seq, int)
    assert seq > 0

    raw_lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert raw_lines, "expected at least one WAL entry"
    entry = json.loads(raw_lines[0])
    assert entry["kind"] == "plan_step_started"
    assert entry["target"] == "default"
    assert entry["plan_id"] == "p001"
    assert entry["step_id"] == "s1"
    assert entry["depends_on"] == ["s0"]
    assert entry["n_tools"] == 2
    assert entry["seq"] == seq


@pytest.mark.asyncio
async def test_record_plan_step_completed_appends_wal(tmp_path: Path) -> None:
    """Tier 2: record_plan_step_completed writes a WAL entry with content_len."""
    journal, log = _make_journal(tmp_path)
    seq = await journal.record_plan_step_completed(
        plan_id="p001", step_id="s1", content_len=350,
    )
    assert isinstance(seq, int)
    entry = json.loads(log.path.read_text().strip().splitlines()[0])
    assert entry["kind"] == "plan_step_completed"
    assert entry["plan_id"] == "p001"
    assert entry["step_id"] == "s1"
    assert entry["content_len"] == 350


@pytest.mark.asyncio
async def test_record_plan_step_failed_appends_wal(tmp_path: Path) -> None:
    """Tier 2: record_plan_step_failed writes a WAL entry with error repr."""
    journal, log = _make_journal(tmp_path)
    seq = await journal.record_plan_step_failed(
        plan_id="p001", step_id="s1", error="RuntimeError('boom')",
    )
    assert isinstance(seq, int)
    entry = json.loads(log.path.read_text().strip().splitlines()[0])
    assert entry["kind"] == "plan_step_failed"
    assert entry["plan_id"] == "p001"
    assert entry["step_id"] == "s1"
    assert entry["error"] == "RuntimeError('boom')"


@pytest.mark.asyncio
async def test_record_methods_no_op_without_state_log(tmp_path: Path) -> None:
    """Tier 2: when state_log is None (= test stub) record returns None
    and doesn't raise."""
    journal = SnapshotJournal(
        agent_name="default",
        snapshot_path=tmp_path / "snapshot.json",
        state_log=None,
    )
    assert (
        await journal.record_plan_step_started(
            plan_id="p001", step_id="s1", depends_on=[], n_tools=0,
        )
        is None
    )
    assert (
        await journal.record_plan_step_completed(
            plan_id="p001", step_id="s1", content_len=1,
        )
        is None
    )
    assert (
        await journal.record_plan_step_failed(
            plan_id="p001", step_id="s1", error="x",
        )
        is None
    )


@pytest.mark.asyncio
async def test_record_methods_bump_applied_seq_on_snapshot(tmp_path: Path) -> None:
    """Tier 2: each WAL append bumps applied_seq on the in-memory
    AgentSnapshot so replay-after-crash skips already-applied events
    (= ADR-0001 watermark invariant)."""
    journal, log = _make_journal(tmp_path)
    snap_before = journal.snapshot.applied_seq

    seq1 = await journal.record_plan_step_started(
        plan_id="p001", step_id="s1", depends_on=[], n_tools=0,
    )
    assert journal.snapshot.applied_seq == seq1

    seq2 = await journal.record_plan_step_completed(
        plan_id="p001", step_id="s1", content_len=1,
    )
    assert journal.snapshot.applied_seq == seq2
    assert seq2 > seq1
    assert seq1 > snap_before


# ── interaction with apply_events ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_events_skips_plan_step_kinds_without_state_mutation(
    tmp_path: Path,
) -> None:
    """Tier 2: agent-level apply_events is a no-op for plan_step_*
    (= they don't mutate agent fields, only bump applied_seq). The actual
    per-plan state lives on PlanSnapshot, not AgentSnapshot."""
    journal, log = _make_journal(tmp_path)

    seq1 = await journal.record_plan_step_started(
        plan_id="p001", step_id="s1", depends_on=[], n_tools=0,
    )
    seq2 = await journal.record_plan_step_completed(
        plan_id="p001", step_id="s1", content_len=10,
    )

    from reyn.core.events.agent_snapshot import AgentSnapshot

    snap = AgentSnapshot.empty("default")
    events = list(log.iter_from(0))
    snap.apply_events(events)
    assert snap.applied_seq == seq2
    assert snap.active_plan_ids == []  # plan_step_* don't touch this
    assert snap.active_skill_run_ids == []


@pytest.mark.asyncio
async def test_replay_preserves_step_event_ordering(tmp_path: Path) -> None:
    """Tier 2: WAL preserves seq order across plan_started + step events
    + plan_completed (= analyzer can deterministically reconstruct
    pairings)."""
    journal, log = _make_journal(tmp_path)

    await journal.record_plan_started(plan_id="p001", goal="g", n_steps=2)
    s1_started = await journal.record_plan_step_started(
        plan_id="p001", step_id="s1", depends_on=[], n_tools=0,
    )
    s1_completed = await journal.record_plan_step_completed(
        plan_id="p001", step_id="s1", content_len=10,
    )
    s2_started = await journal.record_plan_step_started(
        plan_id="p001", step_id="s2", depends_on=["s1"], n_tools=0,
    )
    s2_failed = await journal.record_plan_step_failed(
        plan_id="p001", step_id="s2", error="err",
    )
    await journal.record_plan_completed(plan_id="p001")

    events = list(log.iter_from(0))
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert s1_started < s1_completed < s2_started < s2_failed
    kinds = [e["kind"] for e in events]
    assert kinds == [
        "plan_started",
        "plan_step_started",
        "plan_step_completed",
        "plan_step_started",
        "plan_step_failed",
        "plan_completed",
    ]
