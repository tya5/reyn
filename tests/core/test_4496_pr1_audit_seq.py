"""Tier 2: #4496 PR-1 — audit-event monotonic sequencing (contract 3).

Architect's own witness list (issue comment): (1) same emitter, consecutive
events, audit_seq +1; (2) a different emitter starts at 1 without disturbing
the first emitter's own continuity; (3) the caller cannot influence numbering
(emit has no seq-accepting parameter, and a caller-supplied same-named key
is overwritten, never honored).
"""
from __future__ import annotations

from reyn.core.events.events import EventLog, emit_cli_event


def test_same_emitter_consecutive_events_increment_by_one():
    """Tier 2: witness ① — two emits on the SAME EventLog get audit_seq 1, 2."""
    log = EventLog()
    e1 = log.emit("tool_executed", op="read")
    e2 = log.emit("tool_executed", op="read")
    assert e1.data["audit_seq"] == 1
    assert e2.data["audit_seq"] == 2
    assert e1.data["emitter"] == e2.data["emitter"]


def test_a_different_emitter_starts_at_one_independently():
    """Tier 2: witness ② — a second EventLog (= a different execution
    instance) starts its own count at 1, and the first EventLog's
    continuity is unaffected by the second's existence."""
    log_a = EventLog()
    log_b = EventLog()

    a1 = log_a.emit("tool_executed", op="read")
    b1 = log_b.emit("tool_executed", op="read")
    a2 = log_a.emit("tool_executed", op="read")

    assert a1.data["audit_seq"] == 1
    assert a2.data["audit_seq"] == 2
    assert b1.data["audit_seq"] == 1
    assert a1.data["emitter"] != b1.data["emitter"]


def test_two_fresh_event_logs_get_different_emitter_tokens():
    """Tier 2: (accept-side) construction-time uniqueness — two EventLog
    instances constructed with no explicit emitter never collide, the
    property this whole design leans on (a fresh instance per real
    process execution)."""
    instances = [EventLog() for _ in range(50)]
    tokens = {log.emitter for log in instances}
    assert len(tokens) == len(instances), "two fresh EventLogs collided on emitter"


def test_caller_cannot_supply_audit_seq_and_have_it_win():
    """Tier 2: witness ③ — a caller passing audit_seq=... in the emit call
    does NOT win; the EventLog's own counter always overwrites it. This is
    the falsifiable form of "the caller cannot influence numbering" — if
    emit ever became caller-wins for this field (the same convention
    agent_id/run_id use), this test catches it."""
    log = EventLog()
    log.emit("tool_executed", op="read")  # audit_seq 1, consumed
    forged = log.emit("tool_executed", op="read", audit_seq=9999)
    assert forged.data["audit_seq"] == 2


def test_caller_cannot_supply_emitter_and_have_it_win():
    """Tier 2: same non-caller-wins guarantee for the emitter field itself
    — a forged emitter string must not let an event masquerade as
    belonging to a different execution's sequence."""
    log = EventLog(emitter="real-emitter")
    forged = log.emit("tool_executed", op="read", emitter="someone-else")
    assert forged.data["emitter"] == "real-emitter"


def test_emit_has_no_seq_accepting_keyword_parameter():
    """Tier 2: witness ③, mechanical form — emit's own signature has no
    audit_seq/emitter positional/keyword slot a caller could target; both
    only ever arrive via **data, which the two tests above already prove
    gets overwritten. This test would fail if a future edit turned emit
    into `def emit(self, type, *, audit_seq=None, **data)`, silently
    reopening the caller-wins path."""
    import inspect

    sig = inspect.signature(EventLog.emit)
    assert "audit_seq" not in sig.parameters
    assert "emitter" not in sig.parameters


def test_audit_seq_is_a_distinct_name_from_the_wal_seq():
    """Tier 2: architect's naming mandate, falsified directly — the key is
    literally 'audit_seq', never bare 'seq' (which the WAL already owns
    for a different concept)."""
    log = EventLog()
    event = log.emit("tool_executed", op="read")
    assert "audit_seq" in event.data
    assert "seq" not in event.data


def test_emitter_is_explicitly_overridable_at_construction():
    """Tier 2: (accept-side) a caller MAY set emitter at construction time
    (unlike per-emit-call, which is locked) — the seam emit_cli_event uses
    for its own "cli" label."""
    log = EventLog(emitter="my-label")
    event = log.emit("tool_executed", op="read")
    assert event.data["emitter"] == "my-label"


# ── emit_cli_event: one-off, no continuity ───────────────────────────────


def test_cli_event_carries_the_cli_emitter_label(tmp_path, monkeypatch):
    """Tier 2: emit_cli_event's own events carry emitter='cli', a legible
    label rather than a random per-call token — architect's ruling."""
    (tmp_path / ".reyn").mkdir()
    monkeypatch.chdir(tmp_path)
    emit_cli_event("secret_set", key="X")

    written = list((tmp_path / ".reyn" / "events" / "direct" / "cli").rglob("*.jsonl"))
    assert written
    content = written[0].read_text(encoding="utf-8")
    assert '"emitter": "cli"' in content or '"emitter":"cli"' in content


def test_cli_event_carries_no_audit_seq(tmp_path, monkeypatch):
    """Tier 2: architect's ruling, verbatim — a one-off CLI event has no
    continuity to protect, so audit_seq is omitted entirely rather than
    always stamping a meaningless 1."""
    (tmp_path / ".reyn").mkdir()
    monkeypatch.chdir(tmp_path)
    emit_cli_event("secret_set", key="X")

    written = list((tmp_path / ".reyn" / "events" / "direct" / "cli").rglob("*.jsonl"))
    content = written[0].read_text(encoding="utf-8")
    assert "audit_seq" not in content
