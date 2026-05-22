"""Tier 2: ``execute_op`` emits per-op ``tool_called`` / ``tool_completed``
events (issue #427 L4 step 2).

The TUI ToolCallRow widget (= PR #429 PoC) needs per-op lifecycle
events to drive mount + state transitions. This module pins the
event-emission contract so the forwarder + conv pane wiring (= L4
steps 3-4) can reliably subscribe.

Contract pinned here:

1. Successful dispatch emits ``tool_called`` (before handler) and
   ``tool_completed`` with ``status="success"`` (after handler).
2. The ``op_id`` field matches between the start and end events so
   consumers can pair start/end without ambiguity.
3. ``args_summary`` is present on ``tool_called`` and captures
   user-visible op fields (= excludes bulky body fields).
4. ``result_summary`` is present on ``tool_completed`` success and
   captures non-bulky result fields.
5. ``duration_s`` is present + non-negative on ``tool_completed``.
6. Pure ops (= ``OpPurity.pure``, e.g. ``lint``) skip lifecycle
   emission entirely — keeps event log volume bounded on cheap ops.
7. Failure paths (= ``OpSkipped`` / ``Exception``) still emit
   ``tool_completed`` with ``status`` set to the failure mode.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.events.events import EventLog  # noqa: E402
from reyn.op_runtime import execute_op  # noqa: E402
from reyn.op_runtime.context import OpContext  # noqa: E402
from reyn.op_runtime.result import OpSkipped  # noqa: E402
from reyn.permissions.permissions import PermissionDecl  # noqa: E402
from reyn.schemas.models import LintIROp, ShellIROp  # noqa: E402


def _make_ctx(events: EventLog) -> OpContext:
    """Minimal OpContext sufficient for execute_op's dispatch surface."""
    return OpContext(
        workspace=None,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        skill_name="test_skill",
        run_id="test-run-1",
        current_phase="resolve",
    )


def _events_of_type(events: EventLog, type_: str) -> list[dict]:
    return [e.data for e in events.all() if e.type == type_]


@pytest.fixture
def temp_handler():
    """Register a temporary handler for shell ops + clean up after.

    The real handlers live in op_runtime submodules; for these tests
    we want a deterministic handler that returns a known result dict
    so the event payload assertions don't depend on subprocess output.
    """
    from reyn.op_runtime import _HANDLERS

    saved_shell = _HANDLERS.get("shell")
    yield
    if saved_shell is not None:
        _HANDLERS["shell"] = saved_shell
    else:
        _HANDLERS.pop("shell", None)


def test_success_emits_tool_called_and_tool_completed_with_matching_op_id(
    temp_handler,
) -> None:
    """Tier 2: success path emits both events; ``op_id`` ties them."""
    from reyn.op_runtime import _HANDLERS

    async def _stub_shell(op, ctx, caller):
        return {"kind": "shell", "status": "ok", "exit_code": 0}

    _HANDLERS["shell"] = _stub_shell

    events = EventLog()
    ctx = _make_ctx(events)
    op = ShellIROp(kind="shell", cmd="echo hi")

    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    called = _events_of_type(events, "tool_called")
    completed = _events_of_type(events, "tool_completed")
    assert len(called) == 1, "tool_called fires exactly once on success"
    assert len(completed) == 1, "tool_completed fires exactly once on success"

    start = called[0]
    end = completed[0]
    assert start["op_id"], "tool_called carries op_id"
    assert start["op_id"] == end["op_id"], "op_id matches start/end"
    assert start["kind"] == "shell"
    assert end["kind"] == "shell"
    assert end["status"] == "success"


def test_tool_called_args_summary_includes_user_visible_fields(
    temp_handler,
) -> None:
    """Tier 2: ``args_summary`` carries non-bulky op fields for display."""
    from reyn.op_runtime import _HANDLERS

    async def _stub_shell(op, ctx, caller):
        return {"status": "ok"}

    _HANDLERS["shell"] = _stub_shell

    events = EventLog()
    ctx = _make_ctx(events)
    op = ShellIROp(kind="shell", cmd="ls -la")

    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    called = _events_of_type(events, "tool_called")
    summary = called[0]["args_summary"]
    # The exact format isn't pinned; only that the cmd field shows up.
    assert "cmd=" in summary
    assert "ls -la" in summary


def test_tool_completed_carries_duration_and_result_summary(
    temp_handler,
) -> None:
    """Tier 2: ``tool_completed`` has duration + result summary on success."""
    from reyn.op_runtime import _HANDLERS

    async def _stub_shell(op, ctx, caller):
        return {"kind": "shell", "status": "ok", "exit_code": 0, "stderr": ""}

    _HANDLERS["shell"] = _stub_shell

    events = EventLog()
    ctx = _make_ctx(events)
    op = ShellIROp(kind="shell", cmd="true")

    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    end = _events_of_type(events, "tool_completed")[0]
    assert "duration_s" in end
    assert end["duration_s"] >= 0.0
    # Result summary includes the status field; exact format not pinned.
    assert "status=ok" in end["result_summary"] or "status" in end["result_summary"]


def test_pure_op_kind_skips_lifecycle_emission(temp_handler) -> None:
    """Tier 2: ``pure`` ops (e.g. ``lint``) do NOT emit tool_called/completed.

    ``OP_PURITY.pure`` documents that no externally-observable side
    effect distinguishes start/end, so the event log doesn't carry
    these for cheap pure ops.
    """
    from reyn.op_runtime import _HANDLERS

    async def _stub_lint(op, ctx, caller):
        return {"status": "ok"}

    saved = _HANDLERS.get("lint")
    _HANDLERS["lint"] = _stub_lint
    try:
        events = EventLog()
        ctx = _make_ctx(events)
        op = LintIROp(kind="lint", skill_path="reyn/local/test_skill")
        asyncio.run(execute_op(op, ctx, caller="control_ir"))
        assert _events_of_type(events, "tool_called") == []
        assert _events_of_type(events, "tool_completed") == []
    finally:
        if saved is not None:
            _HANDLERS["lint"] = saved
        else:
            _HANDLERS.pop("lint", None)


def test_op_skipped_emits_tool_completed_with_skipped_status(temp_handler) -> None:
    """Tier 2: ``OpSkipped`` failure path still fires ``tool_completed``."""
    from reyn.op_runtime import _HANDLERS

    async def _stub_shell(op, ctx, caller):
        raise OpSkipped("test reason")

    _HANDLERS["shell"] = _stub_shell

    events = EventLog()
    ctx = _make_ctx(events)
    op = ShellIROp(kind="shell", cmd="echo")

    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    completed = _events_of_type(events, "tool_completed")
    assert len(completed) == 1
    assert completed[0]["status"] == "skipped"
    assert completed[0]["error"] == "test reason"


def test_generic_exception_emits_tool_completed_with_failed_status(
    temp_handler,
) -> None:
    """Tier 2: unhandled exception path still fires ``tool_completed``."""
    from reyn.op_runtime import _HANDLERS

    async def _stub_shell(op, ctx, caller):
        raise RuntimeError("boom")

    _HANDLERS["shell"] = _stub_shell

    events = EventLog()
    ctx = _make_ctx(events)
    op = ShellIROp(kind="shell", cmd="echo")

    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    completed = _events_of_type(events, "tool_completed")
    assert len(completed) == 1
    assert completed[0]["status"] == "failed"
    assert "boom" in completed[0]["error"]


def test_bulky_args_field_replaced_with_size_placeholder(temp_handler) -> None:
    """Tier 2: long ``content``-class fields collapse to ``<N chars>``.

    Keeps the event log lean — a 50KB write payload should not balloon
    every ``tool_called`` event with the full body.
    """
    from reyn.op_runtime import _HANDLERS
    from reyn.schemas.models import FileIROp

    async def _stub_file(op, ctx, caller):
        return {"status": "ok"}

    saved = _HANDLERS.get("file")
    _HANDLERS["file"] = _stub_file
    try:
        events = EventLog()
        ctx = _make_ctx(events)
        op = FileIROp(
            kind="file",
            op="write",
            path="/tmp/example.txt",
            content="x" * 5000,
        )
        asyncio.run(execute_op(op, ctx, caller="control_ir"))
        summary = _events_of_type(events, "tool_called")[0]["args_summary"]
        assert "<5000 chars>" in summary, (
            "bulky content field is summarised, not inlined"
        )
        assert "xxxx" not in summary, "raw body must not leak into summary"
    finally:
        if saved is not None:
            _HANDLERS["file"] = saved
        else:
            _HANDLERS.pop("file", None)
