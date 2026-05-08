"""Tier 2 (OS invariant) tests.

These tests guard P1-P8 invariants of the Reyn OS itself. See
docs/deep-dives/contributing/testing.md for the testing policy and tier model.

Each test fails when a core invariant is violated, regardless of how the
violation was introduced. They are intentionally minimal in number (3-5
cases per principle, total ~5-10) — overgrowth here suggests
implementation pinning has crept in under the wrong tier label.
"""
from __future__ import annotations

import pytest

from reyn.events.events import EventLog
from reyn.kernel.normalizer import (
    ControlIRValidationError,
    normalize,
)
from reyn.workspace.workspace import Workspace

# ── P4: LLM output contract ────────────────────────────────────────────────────


def _control(type: str, decision: str, next_phase: str | None, confidence: float = 0.5):
    return {
        "control": {
            "type": type,
            "decision": decision,
            "next_phase": next_phase,
            "confidence": confidence,
            "reason": {"summary": "test"},
        },
        "artifact": {"type": "x", "data": {}},
        "control_ir": [],
    }


def test_p4_transition_requires_next_phase():
    """Tier 2 (P4): control.type='transition' requires non-null next_phase.

    Protects: the LLM output contract documented in CLAUDE.md. A transition
    without a target makes the OS phase-graph state ambiguous.
    """
    raw = _control(type="transition", decision="continue", next_phase=None)
    with pytest.raises(ControlIRValidationError, match="non-empty.*next_phase"):
        normalize(raw, allowed_next_phases=["next_phase"])


def test_p4_finish_forbids_next_phase():
    """Tier 2 (P4): control.type='finish' requires next_phase=null.

    Protects: an LLM that emits both 'finish' and a target next_phase is
    self-contradictory; the OS must reject it rather than pick one
    arbitrarily.
    """
    raw = _control(type="finish", decision="finish", next_phase="some_phase")
    with pytest.raises(ControlIRValidationError, match="next_phase=null"):
        normalize(raw, allowed_next_phases=["some_phase"])


def test_p4_finish_requires_finish_decision():
    """Tier 2 (P4): control.type='finish' requires control.decision='finish'.

    Protects: the OS-level decision vocabulary (continue|finish|abort) must
    be consistent with the control type. 'revise' / other skill-specific
    decisions are not permitted (see CLAUDE.md P7).
    """
    raw = _control(type="finish", decision="continue", next_phase=None)
    with pytest.raises(ControlIRValidationError, match="decision='finish'"):
        normalize(raw, allowed_next_phases=[])


# ── P5: Workspace is the single source of truth ───────────────────────────────


def test_p5_workspace_round_trip(tmp_path, monkeypatch):
    """Tier 2 (P5): data round-trips through Workspace API; no other channel.

    Protects: the principle that Workspace is the only sink/source for
    inter-phase data. A read after a write must return exactly what was
    written — anything else means there's a side channel.
    """
    monkeypatch.chdir(tmp_path)
    ws = Workspace(events=EventLog())

    ws.write_file("artifact.txt", "hello world")
    content, found = ws.read_file("artifact.txt")

    assert found is True
    assert content == "hello world"


def test_p5_workspace_rejects_writes_outside_project(tmp_path, monkeypatch):
    """Tier 2 (P5): Workspace refuses absolute paths outside project root
    when no PermissionResolver has approved them.

    Protects: workspace boundary. Phases / preprocessors cannot exfiltrate
    data to arbitrary filesystem locations and treat that as inter-phase
    state — the data must live within the workspace boundary (CWD by
    default, or explicitly approved paths).
    """
    monkeypatch.chdir(tmp_path)
    ws = Workspace(events=EventLog())  # no permission resolver

    with pytest.raises(PermissionError, match="(write not permitted|escapes project)"):
        ws.write_file("/etc/some_other_dir/leaked.txt", "x")


# ── P6: Events are the audit truth ─────────────────────────────────────────────


def test_p6_workspace_write_emits_event(tmp_path, monkeypatch):
    """Tier 2 (P6): every workspace mutation produces an event.

    Protects: the audit-truth principle. If a state-mutating operation
    can occur without a corresponding event, the events log is no longer
    a complete description of execution and replay/recovery breaks.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    ws = Workspace(events=events)

    ws.write_file("foo.txt", "content")

    types = [e.type for e in events.all()]
    assert "workspace_updated" in types, (
        f"P6 violation: workspace.write_file did not emit workspace_updated event. "
        f"Got events: {types}"
    )


def test_p6_workspace_delete_emits_event(tmp_path, monkeypatch):
    """Tier 2 (P6): workspace deletion is audited.

    Protects: deletion is a state mutation. Even if the file disappears,
    the events log retains a record. Without this, a phase that deletes
    its own scratch artifact would be invisible to replay.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    ws = Workspace(events=events)

    ws.write_file("foo.txt", "x")
    events_before_delete = list(events.all())

    deleted = ws.delete_file("foo.txt")
    assert deleted is True

    # Look for an event emitted *after* the write
    new_events = events.all()[len(events_before_delete):]
    assert any(e.type == "workspace_updated" for e in new_events), (
        f"P6 violation: workspace.delete_file did not emit an event. "
        f"New events: {[e.type for e in new_events]}"
    )
