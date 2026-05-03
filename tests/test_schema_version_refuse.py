"""Tier 2: PR-resume-ux U4 — snapshot schema version mismatch refuse.

Pre-1.0 release policy: when Reyn's snapshot schema changes incompatibly,
we bump ``SNAPSHOT_VERSION`` (and the per-skill counterpart) so that
loading a stale snapshot fails with a clear, actionable error rather than
silently corrupting state with stale fields.

User remediation: run ``reyn chat --reset`` to wipe in-flight skill state
(audit logs preserved). Post-1.0 will add automated migration (R-D15);
until then the explicit reset is the documented upgrade path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.events.agent_snapshot import (
    AgentSnapshot,
    SNAPSHOT_VERSION,
    SchemaVersionError,
)
from reyn.skill.skill_snapshot import (
    SKILL_SNAPSHOT_VERSION,
    SkillSnapshot,
)


# ---------------------------------------------------------------------------
# AgentSnapshot
# ---------------------------------------------------------------------------


def test_agent_snapshot_load_succeeds_for_current_version(tmp_path):
    """Tier 2: normal save → load round-trip works."""
    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 7
    path = tmp_path / "snapshot.json"
    snap.save(path)
    loaded = AgentSnapshot.load("alpha", path)
    assert loaded.applied_seq == 7


def test_agent_snapshot_load_refuses_higher_version(tmp_path):
    """Tier 2: snapshot from a NEWER schema (e.g. future version) is refused.

    Defensive against downgrade scenarios — running an older Reyn binary
    against snapshots written by a newer one would silently drop new
    fields. Better to refuse + tell the user.
    """
    path = tmp_path / "snapshot.json"
    payload = {
        "version": SNAPSHOT_VERSION + 100,  # future version
        "applied_seq": 0,
        "inbox": [],
        "pending_chains": {},
        "active_skill_run_ids": [],
        "outstanding_interventions": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaVersionError, match="version"):
        AgentSnapshot.load("alpha", path)


def test_agent_snapshot_load_refuses_lower_version(tmp_path):
    """Tier 2: snapshot from an older incompatible schema is refused.

    The error message should mention ``--reset`` so the user has clear
    next-action.
    """
    path = tmp_path / "snapshot.json"
    payload = {
        "version": SNAPSHOT_VERSION - 1 if SNAPSHOT_VERSION > 1 else 0,
        "applied_seq": 0,
    }
    if payload["version"] < 0:
        pytest.skip("can't downgrade version below 0")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaVersionError) as exc_info:
        AgentSnapshot.load("alpha", path)
    assert "--reset" in str(exc_info.value)


def test_agent_snapshot_load_missing_version_treated_as_legacy(tmp_path):
    """Tier 2: snapshot without ``version`` field — refuse (pre-stamp era).

    Pre-PR-resume-ux β snapshots have ``version: 1`` already, so this
    only matters for hypothetical legacy / corrupt files. Refuse rather
    than silently treat as version 1 — the user should explicitly --reset.
    """
    path = tmp_path / "snapshot.json"
    payload = {"applied_seq": 0}  # no version field
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaVersionError):
        AgentSnapshot.load("alpha", path)


def test_agent_snapshot_corrupt_file_returns_empty(tmp_path):
    """Tier 2: malformed JSON falls back to empty snapshot (existing behavior).

    Distinct from version mismatch — corrupt files have no version
    info to compare. Defensive empty fallback preserved for backward
    compat.
    """
    path = tmp_path / "snapshot.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    snap = AgentSnapshot.load("alpha", path)
    assert snap.applied_seq == 0


# ---------------------------------------------------------------------------
# SkillSnapshot
# ---------------------------------------------------------------------------


def test_skill_snapshot_load_succeeds_for_current_version(tmp_path):
    """Tier 2: skill snapshot save → load round-trip."""
    snap = SkillSnapshot(
        skill_run_id="run_x", skill_name="demo",
        skill_input={"type": "input", "data": {}},
        applied_seq=3,
    )
    path = tmp_path / "run_x.snapshot.json"
    snap.save(path)
    loaded = SkillSnapshot.load("run_x", path)
    assert loaded.applied_seq == 3
    assert loaded.skill_name == "demo"


def test_skill_snapshot_load_refuses_higher_version(tmp_path):
    """Tier 2: future-version skill snapshot is refused."""
    path = tmp_path / "run_y.snapshot.json"
    payload = {
        "version": SKILL_SNAPSHOT_VERSION + 100,
        "skill_run_id": "run_y",
        "skill_name": "demo",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaVersionError):
        SkillSnapshot.load("run_y", path)


def test_skill_snapshot_load_missing_version_refused(tmp_path):
    """Tier 2: skill snapshot without version → refuse."""
    path = tmp_path / "run_z.snapshot.json"
    path.write_text(json.dumps({"skill_run_id": "run_z"}), encoding="utf-8")

    with pytest.raises(SchemaVersionError):
        SkillSnapshot.load("run_z", path)


# ---------------------------------------------------------------------------
# Error message UX
# ---------------------------------------------------------------------------


def test_schema_version_error_message_mentions_reset(tmp_path):
    """Tier 2: error message must surface --reset as remediation.

    Operators should not have to guess; the error tells them what to do.
    """
    path = tmp_path / "snapshot.json"
    payload = {"version": 9999, "applied_seq": 0}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaVersionError) as exc_info:
        AgentSnapshot.load("alpha", path)
    msg = str(exc_info.value)
    assert "--reset" in msg
    assert str(SNAPSHOT_VERSION) in msg or "expected" in msg
