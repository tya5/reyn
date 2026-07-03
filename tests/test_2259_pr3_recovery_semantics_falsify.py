"""Tier 2: #2259 PR-3 — the relaxed-durability crash-semantics close gates (A.1 + A.2).

The async-decoupled model (PR-2b, merged) updates in-memory state IMMEDIATELY and submits the
durable record fire-and-forget to the serial worker. These tests prove the two load-bearing
consequences the re-spec demands as the #2259 close condition, at the levels the unit
truncate-falsify did NOT cover:

  A.1  in-memory-immediate — an op's in-memory effect is observable BEFORE its durable write
       lands (the responsiveness WHY). Falsify: if the op blocked on durability, the
       before-durable window would not exist.
  A.2  system-level recover-to-last-durable — a full ``restore_all`` restores the durable
       prefix; an op submitted but NOT flushed before a crash is cleanly LOST = a consistent
       prefix. The SYSTEM path beyond the unit ``reconstruct()`` truncate-falsify.

Crash-injection (A.2) is a CONSEQUENCE of the code, not forced externally: run1's event loop
ends with the tail still un-drained in the worker queue (no trailing await → the drainer never
gets a yield → ``asyncio.run`` cancels it → the tail dies with the loop = the volatile in-memory
state the relaxed-durability model says is lost). A copytree-reset was rejected: it would delete
the tail regardless of async-vs-blocking, so it could not falsify.

Real StateLog + SnapshotJournal + AgentRegistry + restore_all (no mocks).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# A.1 — in-memory-immediate (op visible BEFORE durable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_visible_in_memory_before_durable(tmp_path):
    """Tier 2: in-memory-immediate — the op's in-memory effect is observable WHILE its durable
    write is still pending (no await between the op and the asserts = the before-durable window).
    RED if the op blocked on durability (last_durable_seq would already have advanced)."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    durable_before = log.last_durable_seq

    # The op: a sync body (no internal await) → returns before the drainer is given a yield.
    await journal.append_inbox(kind="user", payload={"text": "x"})

    # ── the before-durable window: NO await between the op and these asserts ──
    # (a) in-memory reflects the op IMMEDIATELY.
    assert any(m["payload"].get("text") == "x" for m in journal.snapshot.inbox), (
        "in-memory-immediate: the op must be visible in-memory at once"
    )
    # (b) durability has NOT landed: the durable watermark is unchanged AND the WAL file is empty.
    assert log.last_durable_seq == durable_before, (
        f"in-memory-immediate: the op must be visible in-memory WHILE its durable write is still "
        f"pending; last_durable_seq advanced to {log.last_durable_seq} = the op blocked on durability"
    )
    assert list(log.iter_from(0)) == [], "the WAL entry must not be durable before the flush barrier"

    # ── the flush barrier → durability lands ──
    await log.flush()
    assert log.last_durable_seq > durable_before, "flush must advance the durable watermark"
    assert any(e["kind"] == "inbox_put" for e in log.iter_from(0)), "the WAL entry is durable post-flush"


# ---------------------------------------------------------------------------
# A.2 — system-level recover-to-last-durable (full restore_all, un-durable tail)
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path, wal: Path) -> tuple[AgentRegistry, StateLog]:
    """A registry whose sessions share one WAL (= production wiring). A fresh registry over the
    SAME project_root + wal simulates a process restart. Returns the shared StateLog too so the
    caller can ``aclose`` it (cancel the worker drainer) at the end — clean teardown, no leak."""
    state_log = StateLog(wal)

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")  # satisfy the listener-presence guard
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    return reg, state_log


def _iv_dict(iv_id: str, run_id: str) -> dict:
    return {
        "kind": "ask_user", "prompt": f"Q-{iv_id}?", "detail": "", "choices": [],
        "suggestions": [], "run_id": run_id, "actor": "demo", "id": iv_id,
    }


def _active_iv_ids(session: Session) -> list[str]:
    return [iv.id for iv in session.interventions.list_active()]


def _setup_agent(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".reyn" / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    AgentProfile.new("alpha", role="").save(agent_dir)


async def _build_durable_state_wal_ahead(tmp_path: Path, wal: Path) -> None:
    """A durable snapshot base ``{iv_base}`` + two WAL-AHEAD ``intervention_dispatched`` entries
    (iv_wal2, iv_wal3) that are durable IN THE WAL but past the snapshot's applied_seq (the
    FIFO-lag window: the WAL runs ahead of the snapshot). So a WAL truncation visibly drops a real
    op on recovery, rather than passing vacuously off the snapshot."""
    reg, state_log = _make_registry(tmp_path, wal)
    main = reg.get_or_load("alpha")
    # durable base: a full (WAL + snapshot) pair, flushed → snapshot.applied_seq = iv_base's seq.
    await main.journal.record_intervention_dispatched(
        intervention_id="iv_base", iv_dict=_iv_dict("iv_base", "rB"),
    )
    await main.journal.flush()
    # WAL-ahead: raw WAL appends (no snapshot save) → durable in the WAL, snapshot stays at base.
    main.journal._wal_append_nowait(
        "intervention_dispatched", target="alpha",
        intervention_id="iv_wal2", iv_dict=_iv_dict("iv_wal2", "r2"),
    )
    main.journal._wal_append_nowait(
        "intervention_dispatched", target="alpha",
        intervention_id="iv_wal3", iv_dict=_iv_dict("iv_wal3", "r3"),
    )
    await main.journal.flush()
    await state_log.aclose()  # clean teardown: cancel the worker drainer (data already durable)


def _wal_lines(wal: Path) -> list[str]:
    return [line for line in wal.read_text().splitlines() if line.strip()]


async def _restore_ivids(tmp_path: Path, wal: Path) -> list[str]:
    """A fresh registry (= restart) over the same project_root + WAL → restore_all → the restored
    main session's active intervention ids. Flushes the save-back so the durable snapshot is
    consistent for any subsequent restart."""
    reg, state_log = _make_registry(tmp_path, wal)
    await reg.restore_all()
    for _ in range(3):  # let restore_state's re-enqueue tasks register the interventions
        await asyncio.sleep(0)
    main = reg.get_session("alpha")
    ids = [] if main is None else _active_iv_ids(main)
    if main is not None:
        await main.journal.flush()  # drain restore_all's async snapshot save-back
    await state_log.aclose()  # clean teardown: cancel the worker drainer
    return ids


@pytest.mark.asyncio
async def test_restore_all_consistent_prefix_from_clean_wal_truncation(tmp_path, monkeypatch):
    """Tier 2: system recover-to-last-durable — ``restore_all`` recovers a CONSISTENT PREFIX from a
    crash-truncated WAL. A clean line-boundary truncation (the durable tail did not survive the
    crash) drops the WAL-ahead op; base + the surviving WAL-ahead op recover, nothing half-applied.
    RED if restore_all failed to replay the surviving WAL-ahead entry, or over-recovered the dropped
    one."""
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    wal = tmp_path / ".reyn" / "wal.jsonl"
    await _build_durable_state_wal_ahead(tmp_path, wal)

    lines = _wal_lines(wal)
    idx = next(i for i, line in enumerate(lines) if "iv_wal3" in line)
    # clean truncation at a line boundary: keep everything before iv_wal3 (= the crash dropped it).
    wal.write_text("\n".join(lines[:idx]) + "\n")

    restored = await _restore_ivids(tmp_path, wal)
    assert set(restored) == {"iv_base", "iv_wal2"}, (
        f"consistent prefix expected (base + the surviving WAL-ahead op; the truncated one dropped); "
        f"got {restored}"
    )


@pytest.mark.asyncio
async def test_restore_all_consistent_prefix_from_torn_midline_wal_truncation(tmp_path, monkeypatch):
    """Tier 2: system recover-to-last-durable under a TORN (mid-line) write — a power-loss truncation
    lands mid-line, leaving a partial JSON fragment as the WAL tail. ``iter_from`` skips the torn
    fragment (best-effort recovery from whatever survived) → restore_all still produces a consistent
    prefix, no half-applied/torn state; and a re-restart is idempotent. RED if a torn fragment
    crashed recovery or leaked a partial entry into the restored state."""
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    wal = tmp_path / ".reyn" / "wal.jsonl"
    await _build_durable_state_wal_ahead(tmp_path, wal)

    lines = _wal_lines(wal)
    idx = next(i for i, line in enumerate(lines) if "iv_wal3" in line)
    # torn write: iv_wal3's line is truncated mid-way (an incomplete JSON fragment, no newline).
    torn_fragment = lines[idx][: len(lines[idx]) // 2]
    wal.write_text("\n".join(lines[:idx]) + "\n" + torn_fragment)

    restored = await _restore_ivids(tmp_path, wal)
    assert set(restored) == {"iv_base", "iv_wal2"}, (
        f"the torn fragment must be skipped (no crash, no partial entry leaked); got {restored}"
    )
    # idempotent re-restart: restoring again over the same (still-torn) WAL yields the same prefix.
    restored_again = await _restore_ivids(tmp_path, wal)
    assert set(restored_again) == {"iv_base", "iv_wal2"}, (
        f"re-restart must be idempotent (a consistent prefix is a fixed point); got {restored_again}"
    )


# ---------------------------------------------------------------------------
# B — durability_failed consumer (fail-stop + operator-surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durability_failure_fail_stops_and_surfaces(tmp_path):
    """Tier 2: B — a persistent (§4-exhausted) fire-and-forget durable-write failure latches the
    worker health-signal; the session FAIL-STOPS — rejects new ops at the accept-edge (`_put_inbox`
    raises ``DurabilityHaltError`` = the synchronous operator-surface) AND halts the run loop
    (process-edge → stops in-memory advancing) — never silently keeps racing ahead of a dead disk
    (the owner's "no silent unbounded loss"). RED if the consumer is absent (the op is accepted /
    the loop keeps running)."""
    from reyn.core.events.durability_worker import DurabilityWorker
    from reyn.runtime.session import DurabilityHaltError, Session

    worker = DurabilityWorker(max_write_attempts=1)  # fail-fast: no slow backoff in the test
    log = StateLog(tmp_path / "wal.jsonl", worker=worker)
    session = Session(agent_name="alpha", state_log=log)
    try:
        assert not log.durability_failed, "health-signal clear before any failure"

        # inject a persistent fire-and-forget durable-write failure (§4-exhausted, no submitter).
        async def _boom() -> None:
            raise OSError("simulated disk death")

        log.submit_durable_nowait(_boom)
        await log.flush()  # drain → retry-exhaust → latch (never swallowed)
        assert log.durability_failed, "the §4-exhausted fire-and-forget failure must latch the health-signal"

        # (a) accept-edge: `_put_inbox` REJECTS new ops with DurabilityHaltError (= operator-surface).
        with pytest.raises(DurabilityHaltError):
            await session._put_inbox("user", {"text": "after disk death"})

        # (b) process-edge: the run loop HALTS (stops advancing in-memory) + records the reason.
        cont = await session.run_one_iteration()
        assert cont is False, "the run loop must halt on durability failure (no further in-memory advance)"
        assert session.halted_reason == "durability_failure"
    finally:
        await log.aclose()  # clean teardown: cancel the worker drainer
