"""Tests for AgentSnapshot — new fields and apply_events handlers (PR-state-foundation)."""
import json
from pathlib import Path

import pytest

from reyn.events.agent_snapshot import AgentSnapshot


# ── helpers ─────────────────────────────────────────────────────────────────

def _snap(name: str = "agent_x") -> AgentSnapshot:
    return AgentSnapshot.empty(name)


def _event(kind: str, seq: int = 1, **fields) -> dict:
    return {"kind": kind, "seq": seq, "target": "agent_x", **fields}


# ── new field round-trip ──────────────────────────────────────────────────────

def test_new_fields_default_values():
    """Tier 2: new fields default to empty list / empty dict."""
    snap = AgentSnapshot.empty("agent_new")
    assert snap.active_skill_run_ids == []
    assert snap.outstanding_interventions == {}


def test_new_fields_save_load_roundtrip(tmp_path: Path):
    """Tier 2: active_skill_run_ids and outstanding_interventions survive save/load."""
    path = tmp_path / "snapshot.json"
    snap = AgentSnapshot.empty("agent_y")
    snap.active_skill_run_ids = ["run-1", "run-2"]
    snap.outstanding_interventions = {"iv-A": {"question": "ok?"}}

    snap.save(path)
    loaded = AgentSnapshot.load("agent_y", path)

    assert loaded.active_skill_run_ids == ["run-1", "run-2"]
    assert loaded.outstanding_interventions == {"iv-A": {"question": "ok?"}}


def test_old_snapshot_without_new_fields_loads_with_defaults(tmp_path: Path):
    """Tier 2: snapshot written before new fields are absent → default gracefully."""
    path = tmp_path / "snapshot_old.json"
    old_payload = {
        "version": 1,
        "applied_seq": 7,
        "inbox": [],
        "pending_chains": {},
        # active_skill_run_ids and outstanding_interventions intentionally absent
    }
    path.write_text(json.dumps(old_payload), encoding="utf-8")

    snap = AgentSnapshot.load("agent_old", path)
    assert snap.applied_seq == 7
    assert snap.active_skill_run_ids == []
    assert snap.outstanding_interventions == {}


# ── apply_events: skill_started / skill_completed ───────────────────────────

def test_apply_skill_started_adds_run_id():
    """Tier 2: skill_started event appends run_id to active_skill_run_ids."""
    snap = _snap()
    snap.apply_events([_event("skill_started", seq=1, run_id="run-abc")])
    assert "run-abc" in snap.active_skill_run_ids


def test_apply_skill_started_idempotent():
    """Tier 2: duplicate skill_started events don't add run_id twice."""
    snap = _snap()
    snap.apply_events([
        _event("skill_started", seq=1, run_id="run-dup"),
        _event("skill_started", seq=2, run_id="run-dup"),
    ])
    assert snap.active_skill_run_ids.count("run-dup") == 1
    assert snap.applied_seq == 2


def test_apply_skill_completed_removes_run_id():
    """Tier 2: skill_completed removes run_id from active_skill_run_ids."""
    snap = _snap()
    snap.apply_events([
        _event("skill_started", seq=1, run_id="run-xyz"),
        _event("skill_completed", seq=2, run_id="run-xyz"),
    ])
    assert "run-xyz" not in snap.active_skill_run_ids


def test_apply_skill_completed_unknown_run_id_noop():
    """Tier 2: skill_completed for unknown run_id is a no-op (no KeyError)."""
    snap = _snap()
    snap.apply_events([_event("skill_completed", seq=1, run_id="ghost")])
    assert snap.active_skill_run_ids == []


# ── apply_events: intervention_dispatched / intervention_resolved ─────────────

def test_apply_intervention_dispatched_stores_iv():
    """Tier 2: intervention_dispatched stores iv_dict under intervention_id."""
    snap = _snap()
    snap.apply_events([
        _event(
            "intervention_dispatched",
            seq=1,
            intervention_id="iv-001",
            iv_dict={"question": "proceed?"},
        )
    ])
    assert "iv-001" in snap.outstanding_interventions
    assert snap.outstanding_interventions["iv-001"] == {"question": "proceed?"}


def test_apply_intervention_resolved_removes_iv():
    """Tier 2: intervention_resolved removes the entry from outstanding_interventions."""
    snap = _snap()
    snap.apply_events([
        _event(
            "intervention_dispatched",
            seq=1,
            intervention_id="iv-002",
            iv_dict={"q": "x"},
        ),
        _event("intervention_resolved", seq=2, intervention_id="iv-002"),
    ])
    assert "iv-002" not in snap.outstanding_interventions


def test_apply_intervention_resolved_unknown_noop():
    """Tier 2: intervention_resolved for unknown id is a no-op (no KeyError)."""
    snap = _snap()
    snap.apply_events([
        _event("intervention_resolved", seq=1, intervention_id="ghost-iv")
    ])
    assert snap.outstanding_interventions == {}


# ── skill-internal kinds don't mutate agent snapshot ─────────────────────────

def test_skill_internal_kinds_are_noop():
    """Tier 2: skill_phase_advanced, step_* and skill_resumed don't raise or mutate agent fields."""
    snap = _snap()
    events = [
        _event("skill_phase_advanced", seq=1, run_id="r", next_phase="p2"),
        _event("step_started", seq=2, run_id="r", op="file/read"),
        _event("step_completed", seq=3, run_id="r", op="file/read", result="ok"),
        _event("step_failed", seq=4, run_id="r", op="mcp/tool", error="timeout"),
        _event("skill_resumed", seq=5, run_id="r"),
    ]
    snap.apply_events(events)
    # Agent-level tracking fields remain untouched
    assert snap.active_skill_run_ids == []
    assert snap.outstanding_interventions == {}
    assert snap.applied_seq == 5
