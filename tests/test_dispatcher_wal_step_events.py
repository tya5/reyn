"""Tests for dispatch_tool's WAL step-event emission (PR-skill-resume part A).

Tier 2: OS invariant — when a skill_phase caller wires a state_log + run_id
into DispatchContext, dispatch_tool must emit step_started / step_completed
/ step_failed events to the WAL according to OpPurity rules. These events
are the basis for forward-replay resume; missing or wrong-shape events
break recovery correctness.

Observation flows through:
  - the StateLog file content (re-read via iter_from)
  - the dispatch_tool return value
No mocks — we use a real StateLog backed by a tmp_path file.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEvents:
    """Captures audit events from dispatch_tool. Plain Fake, no mock."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


def _make_ctx(
    *,
    state_log: StateLog | None,
    skill_run_id: str | None = "run_test_001",
    phase: str | None = "draft",
    caller_kind: str = "skill_phase",
    catalog: dict | None = None,
) -> tuple[DispatchContext, _FakeEvents]:
    events = _FakeEvents()
    return (
        DispatchContext(
            caller_kind=caller_kind,
            caller_id=f"skill_x.{phase}",
            chain_id="c1",
            tool_catalog=catalog or _CATALOG,
            events=events,
            state_log=state_log,
            skill_run_id=skill_run_id,
            phase=phase,
        ),
        events,
    )


# Catalog: includes ops of varying purity for selective testing.
_CATALOG = {
    # side_effect: emits step_started + step_completed
    "file":     {"function": {"name": "file"}},
    # external: same emission pattern
    "shell":    {"function": {"name": "shell"}},
    # world: step_completed only
    "web_fetch": {"function": {"name": "web_fetch"}},
    # pure: no step events
    "lint":     {"function": {"name": "lint"}},
}


def _wal_kinds(state_log: StateLog) -> list[str]:
    return [e["kind"] for e in state_log.iter_from(0)]


def _wal_entries(state_log: StateLog, kind: str) -> list[dict]:
    return [e for e in state_log.iter_from(0) if e.get("kind") == kind]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_side_effect_op_emits_step_started_and_completed(tmp_path):
    """Tier 2: side_effect op (file) emits both step_started and step_completed in order."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            return {"ok": True, "rows": 3}
        return await dispatch_tool(
            name="file", args={"op": "write", "path": "x.txt", "content": "y"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result["status"] == "ok"

    kinds = _wal_kinds(log)
    assert kinds == ["step_started", "step_completed"]

    started = _wal_entries(log, "step_started")[0]
    assert started["run_id"] == "run_test_001"
    assert started["phase"] == "draft"
    assert started["op_invocation_id"] == "draft.0"
    assert started["op_kind"] == "file"
    assert started["args"] == {"op": "write", "path": "x.txt", "content": "y"}
    assert started["args_hash"]  # non-empty

    completed = _wal_entries(log, "step_completed")[0]
    assert completed["run_id"] == "run_test_001"
    assert completed["op_invocation_id"] == "draft.0"
    assert completed["op_kind"] == "file"
    assert completed["result"] == {"ok": True, "rows": 3}
    assert completed["args_hash"] == started["args_hash"]


def test_world_op_emits_only_step_completed(tmp_path):
    """Tier 2: world-purity op (web_fetch) emits step_completed but not step_started.

    World ops have no side effects, so step_started (used for ambiguity
    detection on resume) is unnecessary. The result is still recorded for
    memoization.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            return {"content": "hello"}
        return await dispatch_tool(
            name="web_fetch", args={"url": "https://example.com"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result["status"] == "ok"
    assert _wal_kinds(log) == ["step_completed"]


def test_pure_op_emits_no_step_events(tmp_path):
    """Tier 2: pure op (lint) emits no step events — re-execution on resume is safe and cheap."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            return {"passed": True}
        return await dispatch_tool(
            name="lint", args={"skill_path": "x"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    asyncio.run(go())
    assert _wal_kinds(log) == []


def test_step_failed_emitted_on_invoker_exception(tmp_path):
    """Tier 2: invoker exception → step_failed entry with error_kind=exception."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            raise ValueError("boom")
        return await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result["status"] == "error"
    assert result["error"]["kind"] == "exception"

    kinds = _wal_kinds(log)
    assert kinds == ["step_started", "step_failed"]

    failed = _wal_entries(log, "step_failed")[0]
    assert failed["error_kind"] == "exception"
    assert "boom" in failed["message"]
    assert failed["op_invocation_id"] == "draft.0"


def test_step_failed_emitted_on_permission_denied(tmp_path):
    """Tier 2: PermissionError → step_failed with error_kind=permission_denied."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            raise PermissionError("nope")
        return await dispatch_tool(
            name="shell", args={"cmd": "echo hi"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result["error"]["kind"] == "permission_denied"

    failed = _wal_entries(log, "step_failed")[0]
    assert failed["error_kind"] == "permission_denied"
    assert "nope" in failed["message"]


def test_no_step_events_when_state_log_unset(tmp_path):
    """Tier 2: skill_phase caller without state_log emits NO step events (backward compat)."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=None, skill_run_id=None, phase=None)
        async def invoker(args):
            return None
        await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker,
        )

    asyncio.run(go())
    assert _wal_kinds(log) == []


def test_no_step_events_for_router_caller(tmp_path):
    """Tier 2: router caller (chat) emits no step events even if state_log wired.

    Chat router catalog tools are not 'ops' in the IR sense — recovery
    semantics don't apply. State log might be wired for inbox/chain
    persistence, but that's separate from skill resume.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(
            state_log=log, caller_kind="router",
            skill_run_id=None,  # routers don't have run_id
        )
        async def invoker(args):
            return None
        await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker,
        )

    asyncio.run(go())
    assert _wal_kinds(log) == []


def test_no_step_events_when_run_id_missing(tmp_path):
    """Tier 2: state_log set but skill_run_id None → no step events (defensive guard)."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log, skill_run_id=None)
        async def invoker(args):
            return None
        await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker,
        )

    asyncio.run(go())
    assert _wal_kinds(log) == []


def test_args_hash_matches_across_started_and_completed(tmp_path):
    """Tier 2: step_started and step_completed share the same args_hash for memoization on resume."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, _ = _make_ctx(state_log=log)
        async def invoker(args):
            return {"k": "v"}
        await dispatch_tool(
            name="file", args={"op": "write", "path": "/p", "content": "c"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="phase_a.7",
        )

    asyncio.run(go())
    started_hash = _wal_entries(log, "step_started")[0]["args_hash"]
    completed_hash = _wal_entries(log, "step_completed")[0]["args_hash"]
    assert started_hash == completed_hash
    assert started_hash, "args_hash must be a non-empty string"


def test_audit_events_still_emitted_alongside_step_events(tmp_path):
    """Tier 2: WAL step-event emission does not displace audit-event emission.

    Audit (events.emit) and recovery (state_log) are independent layers;
    both must fire for a single dispatch.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        ctx, ev = _make_ctx(state_log=log)
        async def invoker(args):
            return {"ok": True}
        await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )
        return ev

    ev = asyncio.run(go())
    audit_types = [t for t, _ in ev.events]
    assert audit_types == ["tool_called", "tool_returned"]
    # Plus the WAL recorded both step events
    assert _wal_kinds(log) == ["step_started", "step_completed"]
