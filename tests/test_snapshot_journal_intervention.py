"""Tier 2: SnapshotJournal contract — record_intervention_* WAL + snapshot pair.

PR-intervention-link L2. Mirrors the existing record_chain_* / record_inbox
patterns. WAL append + snapshot mutation + atomic save, all inside one
method so the public API is the only correct mutation entry point.

Pinned invariants:
  - record_intervention_dispatched: WAL event ``intervention_dispatched``
    appended with intervention_id + iv_dict; snapshot.outstanding_interventions
    gains the entry; applied_seq advances.
  - record_intervention_resolved: WAL event ``intervention_resolved``
    appended with intervention_id; snapshot.outstanding_interventions
    drops the entry; applied_seq advances.
  - state_log=None → no-op (tests / non-chat construction).
  - Multiple dispatches accumulate; partial resolves leave others intact.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _journal(tmp_path: Path, *, with_state_log: bool = True) -> tuple[SnapshotJournal, StateLog | None]:
    snapshot_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    sl = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    j = SnapshotJournal(
        agent_name="alpha", snapshot_path=snapshot_path, state_log=sl,
    )
    return j, sl


def _iv_dict(iid: str, **overrides) -> dict:
    """Build a minimal serialized UserIntervention payload for the WAL."""
    return {
        "kind": "ask_user",
        "prompt": "What's your name?",
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": "run_alpha_001",
        "skill_name": "demo",
        "id": iid,
        **overrides,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_record_intervention_dispatched_appends_wal_and_snapshot(tmp_path):
    """Tier 2: WAL event appended + outstanding_interventions populated."""
    j, sl = _journal(tmp_path)
    iid = "iv_abcd"
    iv = _iv_dict(iid)

    asyncio.run(j.record_intervention_dispatched(intervention_id=iid, iv_dict=iv))

    # WAL has the event
    events = [e for e in sl.iter_from(0) if e["kind"] == "intervention_dispatched"]
    assert len(events) == 1
    ev = events[0]
    assert ev["target"] == "alpha"
    assert ev["intervention_id"] == iid
    assert ev["iv_dict"] == iv

    # Snapshot mutated
    assert j.snapshot.outstanding_interventions[iid] == iv
    assert j.snapshot.applied_seq == ev["seq"]


def test_record_intervention_resolved_removes_from_snapshot(tmp_path):
    """Tier 2: WAL ``intervention_resolved`` + snapshot entry removed."""
    j, sl = _journal(tmp_path)
    iid = "iv_to_resolve"
    asyncio.run(j.record_intervention_dispatched(
        intervention_id=iid, iv_dict=_iv_dict(iid),
    ))
    assert iid in j.snapshot.outstanding_interventions

    asyncio.run(j.record_intervention_resolved(intervention_id=iid))

    # WAL has the resolve event
    events = [e for e in sl.iter_from(0) if e["kind"] == "intervention_resolved"]
    assert len(events) == 1
    assert events[0]["intervention_id"] == iid
    assert events[0]["target"] == "alpha"

    # Entry gone from snapshot
    assert iid not in j.snapshot.outstanding_interventions


def test_record_intervention_dispatched_noop_when_state_log_none(tmp_path):
    """Tier 2: backward compat — no state_log → no WAL, no mutation."""
    j, _ = _journal(tmp_path, with_state_log=False)
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_x", iv_dict=_iv_dict("iv_x"),
    ))
    # Nothing recorded
    assert j.snapshot.outstanding_interventions == {}


def test_record_intervention_resolved_noop_when_state_log_none(tmp_path):
    """Tier 2: backward compat — resolve no-op when state_log is None."""
    j, _ = _journal(tmp_path, with_state_log=False)
    # Should not raise even if id is unknown
    asyncio.run(j.record_intervention_resolved(intervention_id="iv_unknown"))
    assert j.snapshot.outstanding_interventions == {}


def test_multiple_dispatches_accumulate_and_partial_resolves(tmp_path):
    """Tier 2: many in-flight interventions → resolve one → others intact."""
    j, sl = _journal(tmp_path)
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_1", iv_dict=_iv_dict("iv_1", prompt="Q1"),
    ))
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_2", iv_dict=_iv_dict("iv_2", prompt="Q2"),
    ))
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_3", iv_dict=_iv_dict("iv_3", prompt="Q3"),
    ))

    # Resolve the middle one
    asyncio.run(j.record_intervention_resolved(intervention_id="iv_2"))

    assert set(j.snapshot.outstanding_interventions) == {"iv_1", "iv_3"}
    assert j.snapshot.outstanding_interventions["iv_1"]["prompt"] == "Q1"
    assert j.snapshot.outstanding_interventions["iv_3"]["prompt"] == "Q3"


def test_applied_seq_advances_monotonically(tmp_path):
    """Tier 2: every WAL-recorded mutation bumps applied_seq forward."""
    j, _ = _journal(tmp_path)
    seqs = []
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_a", iv_dict=_iv_dict("iv_a"),
    ))
    seqs.append(j.snapshot.applied_seq)
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_b", iv_dict=_iv_dict("iv_b"),
    ))
    seqs.append(j.snapshot.applied_seq)
    asyncio.run(j.record_intervention_resolved(intervention_id="iv_a"))
    seqs.append(j.snapshot.applied_seq)
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3, "each mutation must advance applied_seq"


def test_resolve_unknown_id_does_not_raise(tmp_path):
    """Tier 2: resolving an id not in outstanding is a no-op (idempotent).

    Useful for crash recovery — a duplicate WAL replay must not error
    when the entry was already pruned by an earlier replay.
    """
    j, _ = _journal(tmp_path)
    asyncio.run(j.record_intervention_resolved(intervention_id="iv_unknown"))
    assert j.snapshot.outstanding_interventions == {}


def test_snapshot_persists_to_disk_on_dispatch(tmp_path):
    """Tier 2: snapshot is saved to disk after each mutation (crash safety)."""
    j, _ = _journal(tmp_path)
    asyncio.run(j.record_intervention_dispatched(
        intervention_id="iv_disk", iv_dict=_iv_dict("iv_disk"),
    ))
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
    assert snap_path.is_file()
    raw = json.loads(snap_path.read_text())
    assert "iv_disk" in raw["outstanding_interventions"]
