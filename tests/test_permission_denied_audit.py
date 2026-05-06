"""Tier 2 / Tier 2c — Permission-denied op: workspace immutability + P6 audit events.

Invariants guarded:

P5: A denied op MUST NOT mutate the workspace (file is not written).
P6: Every permission denial MUST emit an event — the OS's audit record of the
    denial is the only machine-readable trace that the gate fired.

Additional P6 invariants:
  - Two sequential ops: first allowed, second denied → exactly one denial event,
    and the first op's side effect is recorded as a separate success event.
  - The denial event carries the op kind so audit tooling can filter by kind.

Design note:
  - ``execute_op`` catches PermissionError from handlers and emits
    ``permission_denied`` (kind, path, reason) before returning status="denied".
  - ``EventLog.all()`` is the public observation surface (P6).
  - File existence / absence on ``tmp_path`` is the public workspace
    observation surface (P5).
  - No unittest.mock, no private-state assertions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import FileIROp
from reyn.workspace.workspace import Workspace

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    """Non-interactive resolver backed by tmp_path as project root."""
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=False,
    )


def _make_ctx(
    tmp_path: Path,
    events: EventLog,
    *,
    resolver: PermissionResolver | None,
    decl: PermissionDecl | None = None,
    skill_name: str = "test_skill",
) -> OpContext:
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=decl or PermissionDecl(),
        permission_resolver=resolver,
        skill_name=skill_name,
    )


def _write_op(path: str, content: str = "hello") -> FileIROp:
    return FileIROp(kind="file", op="write", path=path, content=content)


def _read_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _run(coro):
    return asyncio.run(coro)


# ── Tier 2: P5 — denied write does not mutate workspace ───────────────────────


def test_permission_denied_op_does_not_mutate_workspace(tmp_path, monkeypatch):
    """Tier 2: P5 invariant — a denied file-write op must not create the target file.

    The PermissionResolver denies write access (non-interactive, no approval).
    execute_op must return status='denied' and the file must remain absent.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    # Write to an absolute path outside the default write zone — will be denied.
    target = tmp_path / "secret_output.txt"
    op = _write_op(str(target))

    result = _run(execute_op(op, ctx, caller="control_ir"))

    # P5: workspace is not mutated
    assert not target.exists(), "denied op must not write the file"
    # Result confirms denial
    assert result["status"] == "denied"


# ── Tier 2: P6 — denied op emits exactly one permission_denied event ──────────


def test_permission_denied_emits_p6_event(tmp_path, monkeypatch):
    """Tier 2: P6 invariant — every permission denial emits a 'permission_denied' event.

    Denying a write op must produce exactly one 'permission_denied' event in the
    EventLog.  Audit tooling relies on this event being present for every denial.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    target = tmp_path / "should_not_exist.txt"
    op = _write_op(str(target))

    _run(execute_op(op, ctx, caller="control_ir"))

    denial_events = [e for e in events.all() if e.type == "permission_denied"]
    assert len(denial_events) == 1, (
        f"expected exactly 1 permission_denied event, got {len(denial_events)}: "
        f"{[e.type for e in events.all()]}"
    )


# ── Tier 2: P6 — denial event carries op kind ─────────────────────────────────


def test_permission_denied_event_carries_op_kind(tmp_path, monkeypatch):
    """Tier 2: P6 invariant — permission_denied event payload includes the op kind.

    The event data must include the 'kind' field so audit tooling can filter
    denials by op type without re-parsing the error message.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    target = tmp_path / "no_write.txt"
    op = _write_op(str(target))

    _run(execute_op(op, ctx, caller="control_ir"))

    denial_events = [e for e in events.all() if e.type == "permission_denied"]
    assert denial_events, "permission_denied event must be emitted"
    assert denial_events[0].data.get("kind") == "file", (
        f"expected kind='file' in event data, got: {denial_events[0].data}"
    )


# ── Tier 2c: allow-then-deny — first op executes, second is denied ─────────────


def test_permission_allow_then_deny_only_first_executes(tmp_path, monkeypatch):
    """Tier 2c: two ops — first allowed (inside CWD), second denied (outside zone).

    P5: only the first op's file should exist.
    P6: exactly one permission_denied event for the second op; the first op's
        tool_executed event is present (confirming it ran).
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    # Config grants write everywhere — but absolute external paths are still
    # blocked by Workspace._resolve_write (absolute path check).
    # Use config-allowed write for CWD, default-denied for outside.
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    # Op 1: write inside CWD — allowed by default write zone heuristic.
    # .reyn/ is the canonical default write zone.
    allowed_target = ".reyn/op1_output.txt"
    op1 = _write_op(allowed_target)

    # Op 2: write to absolute path outside project — denied.
    denied_target = tmp_path / "op2_denied.txt"
    op2 = _write_op(str(denied_target))

    result1 = _run(execute_op(op1, ctx, caller="control_ir"))
    result2 = _run(execute_op(op2, ctx, caller="control_ir"))

    # P5: first op wrote, second did not
    assert result1["status"] == "ok", f"first op should succeed; got {result1}"
    assert (tmp_path / ".reyn" / "op1_output.txt").exists(), "first op file must exist"
    assert not denied_target.exists(), "second (denied) op must not create file"

    # P6: exactly one denial event
    denial_events = [e for e in events.all() if e.type == "permission_denied"]
    assert len(denial_events) == 1, (
        f"expected 1 denial event, got {len(denial_events)}: {[e.type for e in events.all()]}"
    )

    # P6: first op emitted a tool_executed event (audit of successful execution)
    executed_events = [e for e in events.all() if e.type == "tool_executed"]
    assert len(executed_events) >= 1, (
        "first allowed op must emit a tool_executed event (P6 audit truth)"
    )


# ── Tier 2: P6 — denied read emits permission_denied event ───────────────────


def test_permission_denied_read_emits_p6_event(tmp_path, monkeypatch):
    """Tier 2: P6 invariant — denied read op also emits 'permission_denied' event.

    Read-class ops are gated identically to write-class (PR36). The deny event
    must appear in the EventLog for both classes.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    # Absolute path outside CWD — denied for read.
    op = _read_op("/etc/passwd")

    result = _run(execute_op(op, ctx, caller="control_ir"))

    assert result["status"] == "denied"

    denial_events = [e for e in events.all() if e.type == "permission_denied"]
    assert len(denial_events) == 1, (
        f"denied read must emit permission_denied event; got: {[e.type for e in events.all()]}"
    )
    assert denial_events[0].data.get("kind") == "file"


# ── Tier 2: P6 — no spurious events emitted on allowed op ─────────────────────


def test_allowed_op_emits_no_denial_event(tmp_path, monkeypatch):
    """Tier 2: P6 invariant — successful op must not emit 'permission_denied' events.

    A false-positive denial event would corrupt the audit log and cause incorrect
    alerts in audit tooling.  This guards the negative case of the event contract.
    """
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(tmp_path, events, resolver=resolver)

    # Write inside .reyn/ — always allowed.
    op = _write_op(".reyn/allowed_file.txt")

    result = _run(execute_op(op, ctx, caller="control_ir"))

    assert result["status"] == "ok"

    denial_events = [e for e in events.all() if e.type == "permission_denied"]
    assert len(denial_events) == 0, (
        f"successful op must emit zero permission_denied events; "
        f"got {len(denial_events)}: {denial_events}"
    )
