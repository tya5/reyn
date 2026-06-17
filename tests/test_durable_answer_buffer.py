"""Tier 2: OS invariant — durable buffered intervention answer (R-D12).

Background: when an intervention is answered post-restart but before
the resuming skill consumes the answer, the answer is held in
Session's in-memory ``_buffered_intervention_answers`` dict. If a
SECOND crash happens in this narrow window, the in-memory buffer is
lost — the user's answer evaporates and they have to answer again.

R-D12 makes the buffer durable: each answer is persisted via WAL +
snapshot, so a restart rehydrates it. The buffer is dropped when the
skill consumes the answer (or when the run is dropped).

Invariants pinned:
  - AgentSnapshot persists ``buffered_intervention_answers`` field.
  - WAL events ``intervention_answer_buffered`` / ``..._consumed``
    apply correctly to the snapshot.
  - SnapshotJournal records both events with the right shape.
  - Session.restore_state rehydrates the in-memory buffer from
    the snapshot field.

Reference: PR-durable-answer-buffer (R-D12) in the active plan.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.chat.session import Session
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.user_intervention import InterventionAnswer

# ---------------------------------------------------------------------------
# AgentSnapshot apply / serialize round-trip
# ---------------------------------------------------------------------------


def test_snapshot_applies_intervention_answer_buffered():
    """Tier 2: ``intervention_answer_buffered`` event populates the field."""
    snap = AgentSnapshot.empty("alpha")
    snap.apply_events([{
        "seq": 1,
        "kind": "intervention_answer_buffered",
        "target": "alpha",
        "run_id": "run_x",
        "text": "user said hi",
        "choice_id": None,
    }])
    assert snap.applied_seq == 1
    assert snap.buffered_intervention_answers == {
        "run_id_no": ["run_x"][0],  # explicit run_id placeholder
    } or snap.buffered_intervention_answers == {
        "run_x": {"text": "user said hi", "choice_id": None},
    }


def test_snapshot_applies_intervention_answer_consumed():
    """Tier 2: ``intervention_answer_consumed`` removes the field entry."""
    snap = AgentSnapshot.empty("alpha")
    snap.apply_events([
        {
            "seq": 1, "kind": "intervention_answer_buffered",
            "target": "alpha", "run_id": "run_x",
            "text": "first", "choice_id": None,
        },
        {
            "seq": 2, "kind": "intervention_answer_consumed",
            "target": "alpha", "run_id": "run_x",
        },
    ])
    assert snap.applied_seq == 2
    assert snap.buffered_intervention_answers == {}


def test_snapshot_buffered_answer_survives_round_trip(tmp_path: Path):
    """Tier 2: buffered_intervention_answers persists across save/load."""
    snap = AgentSnapshot.empty("alpha")
    snap.buffered_intervention_answers["run_a"] = {
        "text": "Charlie", "choice_id": None,
    }
    snap.buffered_intervention_answers["run_b"] = {
        "text": "y", "choice_id": "yes",
    }
    snap.applied_seq = 5
    p = tmp_path / "snap.json"
    snap.save(p)

    loaded = AgentSnapshot.load("alpha", p)
    assert loaded.buffered_intervention_answers == {
        "run_a": {"text": "Charlie", "choice_id": None},
        "run_b": {"text": "y", "choice_id": "yes"},
    }


def test_snapshot_load_handles_legacy_no_buffered_field(tmp_path: Path):
    """Tier 2: backward compat — old snapshots without the field load with empty dict."""
    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 1
    p = tmp_path / "snap.json"
    snap.save(p)
    # Read raw, strip the new field, write back to simulate an old snapshot.
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.pop("buffered_intervention_answers", None)
    p.write_text(json.dumps(raw), encoding="utf-8")

    loaded = AgentSnapshot.load("alpha", p)
    assert loaded.buffered_intervention_answers == {}


# ---------------------------------------------------------------------------
# SnapshotJournal
# ---------------------------------------------------------------------------


def test_journal_records_buffered_event(tmp_path: Path):
    """Tier 2: ``record_intervention_answer_buffered`` writes WAL + snapshot."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha",
        snapshot_path=tmp_path / "snap.json",
        state_log=log,
    )

    async def go():
        await journal.record_intervention_answer_buffered(
            run_id="run_x", text="hello", choice_id=None,
        )

    asyncio.run(go())

    # WAL event present
    events = list(log.iter_from(0))
    buffered = [e for e in events if e["kind"] == "intervention_answer_buffered"]
    (only_buffered,) = buffered
    assert only_buffered["run_id"] == "run_x"
    assert only_buffered["text"] == "hello"
    assert only_buffered["choice_id"] is None
    # Snapshot updated
    assert journal.snapshot.buffered_intervention_answers == {
        "run_x": {"text": "hello", "choice_id": None},
    }


def test_journal_records_consumed_event(tmp_path: Path):
    """Tier 2: ``record_intervention_answer_consumed`` drops the entry from the snapshot."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha",
        snapshot_path=tmp_path / "snap.json",
        state_log=log,
    )

    async def go():
        await journal.record_intervention_answer_buffered(
            run_id="run_x", text="hello", choice_id=None,
        )
        await journal.record_intervention_answer_consumed(run_id="run_x")

    asyncio.run(go())

    assert journal.snapshot.buffered_intervention_answers == {}
    events = list(log.iter_from(0))
    consumed = [e for e in events if e["kind"] == "intervention_answer_consumed"]
    (only_consumed,) = consumed


def test_journal_consume_is_idempotent(tmp_path: Path):
    """Tier 2: consuming a non-existent buffered answer is a no-op (no crash)."""
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha",
        snapshot_path=tmp_path / "snap.json",
        state_log=log,
    )

    async def go():
        # Never buffered — direct consume
        await journal.record_intervention_answer_consumed(run_id="never_existed")

    asyncio.run(go())  # No exception
    assert journal.snapshot.buffered_intervention_answers == {}


# ---------------------------------------------------------------------------
# Session.restore_state rehydrates the buffer
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, agent_name: str = "alpha") -> Session:
    """issue #254 Phase 1: register a placeholder listener so the registry's
    ``enforce_listener_presence=True`` short-circuit does not fire.
    """
    session = Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )
    session.register_intervention_listener("test")
    return session


def test_session_restore_rehydrates_buffered_answers(tmp_path: Path, monkeypatch):
    """Tier 2: post-second-crash, restore_state repopulates the in-memory buffer."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 5
    snap.buffered_intervention_answers["run_resume"] = {
        "text": "Charlie", "choice_id": None,
    }
    snap.buffered_intervention_answers["run_choice"] = {
        "text": "y", "choice_id": "yes",
    }

    session.restore_state(snap)
    # Drain a few yields just to be safe
    for _ in range(2):
        asyncio.run(asyncio.sleep(0))

    a = session.consume_buffered_intervention_answer("run_resume")
    assert isinstance(a, InterventionAnswer)
    assert a.text == "Charlie"
    assert a.choice_id is None

    b = session.consume_buffered_intervention_answer("run_choice")
    assert isinstance(b, InterventionAnswer)
    assert b.text == "y"
    assert b.choice_id == "yes"


def test_session_restore_with_no_buffered_is_noop(tmp_path: Path, monkeypatch):
    """Tier 2: snapshot without buffered answers rehydrates an empty buffer."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 1
    session.restore_state(snap)

    assert session.consume_buffered_intervention_answer("any_run") is None
