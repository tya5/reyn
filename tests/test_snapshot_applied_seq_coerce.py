"""Tier 2: snapshot load coerces a malformed applied_seq (deser-audit, #1906 pattern).

``AgentSnapshot.load`` / ``PlanSnapshot.load`` read a version-matched JSON file and
do ``applied_seq=int(data.get("applied_seq", 0))``. A hand-edited / corrupted file
with ``applied_seq: null`` or a non-numeric value crashed AFTER passing the version
gate (the ``.get`` default only covers a *missing* key). Coerce-to-default closes
the gap. (File-level corruption — bad JSON / non-dict / version mismatch — is
already handled upstream; this is the surviving in-object case.)

Policy: real load via a temp file, no mocks. Tier line first.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import SNAPSHOT_VERSION, AgentSnapshot
from reyn.core.plan.plan_snapshot import PLAN_SNAPSHOT_VERSION, PlanSnapshot


@pytest.mark.parametrize("bad", [None, "abc", []])
def test_agent_snapshot_malformed_applied_seq(tmp_path: Path, bad) -> None:
    """Tier 2: null / non-numeric applied_seq → 0 (no crash, version-matched)."""
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"version": SNAPSHOT_VERSION, "applied_seq": bad}))
    assert AgentSnapshot.load("a", p).applied_seq == 0


def test_agent_snapshot_valid_applied_seq_preserved(tmp_path: Path) -> None:
    """Tier 2: (regression) a valid applied_seq survives the load."""
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"version": SNAPSHOT_VERSION, "applied_seq": 42}))
    assert AgentSnapshot.load("a", p).applied_seq == 42


@pytest.mark.parametrize("bad", [None, "abc"])
def test_plan_snapshot_malformed_seqs(tmp_path: Path, bad) -> None:
    """Tier 2: null / non-numeric applied_seq + last_step_applied_seq → 0."""
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({
        "schema_version": PLAN_SNAPSHOT_VERSION,
        "applied_seq": bad,
        "last_step_applied_seq": bad,
    }))
    snap = PlanSnapshot.load("pid", p)
    assert snap.applied_seq == 0
    assert snap.last_step_applied_seq == 0
