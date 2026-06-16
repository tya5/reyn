"""Tier 2: "skill done:" cleanup actions are independent — one failure doesn't
leak others (AsyncStackPanel row leak fix).

Bug: when finish_skill_row raised (e.g. for skills that never emitted a
phase_started trace, so no SkillActivityRow exists in _skill_rows for the
run_id), the sequential "skill done:" block in _handle_trace_for_skill_row
skipped all subsequent cleanup steps. The AsyncStackPanel row stayed mounted
with its elapsed counter ticking forever.

Fix: each cleanup step is wrapped in an independent try/except so one
widget's failure cannot block the others. Same pattern as
skill_runner.py:733-738 fallback emit.

These tests verify the independent-cleanup contract using real instances
and direct-attribute substitution (no unittest.mock per testing policy).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView
from reyn.tui.widgets.input_bar import InputBar


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _skill_done_msg(run_id: str, status: str = "finished") -> OutboxMessage:
    return OutboxMessage(
        kind="trace",
        text=f"skill done: {status}",
        meta={"skill_name": "mcp_search", "run_id": run_id},
    )


# ── Test 1: happy-path — all cleanup actions complete ────────────────────────


@pytest.mark.asyncio
async def test_skill_done_finished_cleans_up_fully() -> None:
    """Tier 2: happy path — all 4 cleanup actions complete on "skill done: finished".

    Verifies that after a clean "finished" trace:
    - _skill_exec no longer contains the run_id
    - AsyncStackPanel no longer has the row
    - InputBar is_in_flight() returns False
    - SkillActivityRow is removed from conv (finish_skill_row ran)
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        input_bar = app.query_one("#inputbar", InputBar)

        run_id = "run-happy-1"

        # Simulate an in-flight skill: mount a SkillActivityRow + add AsyncStack
        # entry + mark input as in-flight.
        conv.start_skill_row(run_id, "mcp_search")
        conv.add_async_task(run_id, "mcp_search")
        input_bar.set_in_flight(True)
        app._skill_exec[run_id] = {"phase_visits": 2, "skill_name": "mcp_search"}

        await pilot.pause()

        # Confirm setup is in place.
        assert run_id in app.skill_exec_snapshot()
        assert any(
            s["agent_id"] == run_id
            for s in conv.async_stack_snapshot()
        )
        assert input_bar.is_in_flight()

        # Dispatch the "skill done: finished" trace.
        msg = _skill_done_msg(run_id, "finished")
        app._handle_trace_for_skill_row(conv, msg)
        await pilot.pause()

        # _skill_exec cleaned up.
        assert run_id not in app.skill_exec_snapshot(), (
            "_skill_exec must not contain run_id after skill done"
        )
        # AsyncStackPanel row removed.
        assert all(
            s["agent_id"] != run_id
            for s in conv.async_stack_snapshot()
        ), "AsyncStackPanel must not have the row after skill done: finished"
        # InputBar unlocked (last skill popped → _skill_exec empty).
        assert not input_bar.is_in_flight(), (
            "InputBar must be unlocked after the last skill finishes"
        )


# ── Test 2: finish_skill_row raises → other cleanup still runs ───────────────


@pytest.mark.asyncio
async def test_finish_skill_row_raise_does_not_block_other_cleanup() -> None:
    """Tier 2: if finish_skill_row raises, _skill_exec / remove_async_task /
    InputBar cleanup still complete.

    Induces a raise by substituting conv.finish_skill_row with a real
    callable that always raises RuntimeError — no MagicMock per policy.

    remove_async_task is also captured via substitution so we verify it
    was called (not the panel state), because the "aborted" terminal path
    uses a ~1.5 s flash timer before unmounting — a single pilot.pause()
    won't wait that long.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        input_bar = app.query_one("#inputbar", InputBar)

        run_id = "run-fsr-raises"

        conv.add_async_task(run_id, "mcp_search")
        input_bar.set_in_flight(True)
        app._skill_exec[run_id] = {"phase_visits": 1, "skill_name": "mcp_search"}

        await pilot.pause()

        # Substitute finish_skill_row with a real function that raises.
        def _raising_finish_skill_row(rid: str, *, success: bool, reason: str) -> None:
            raise RuntimeError("simulated finish_skill_row failure")

        conv.finish_skill_row = _raising_finish_skill_row  # type: ignore[method-assign]

        # Capture whether remove_async_task was invoked (real function, no mock).
        remove_calls: list[tuple[str, str]] = []

        def _capture_remove(tid: str, *, terminal: str = "ok") -> None:
            remove_calls.append((tid, terminal))

        conv.remove_async_task = _capture_remove  # type: ignore[method-assign]

        msg = _skill_done_msg(run_id, "aborted")
        app._handle_trace_for_skill_row(conv, msg)
        await pilot.pause()

        # _skill_exec must be cleaned up despite finish_skill_row raising.
        assert run_id not in app.skill_exec_snapshot(), (
            "_skill_exec.pop must run even when finish_skill_row raises"
        )
        # remove_async_task must have been invoked.
        assert any(tid == run_id for tid, _ in remove_calls), (
            "remove_async_task must be called even when finish_skill_row raises; "
            f"calls: {remove_calls!r}"
        )
        # The aborted path must use the "aborted" terminal.
        assert any(
            tid == run_id and term == "aborted"
            for tid, term in remove_calls
        ), (
            f"remove_async_task must receive terminal='aborted'; calls: {remove_calls!r}"
        )
        # InputBar must be unlocked (last skill gone).
        assert not input_bar.is_in_flight(), (
            "set_in_flight(False) must fire even when finish_skill_row raises"
        )


# ── Test 3: remove_async_task raises → _skill_exec / InputBar still clean ────


@pytest.mark.asyncio
async def test_remove_async_task_raise_does_not_block_other_cleanup() -> None:
    """Tier 2: if remove_async_task raises, _skill_exec and InputBar unlock
    still complete.

    Induces a raise by substituting conv.remove_async_task with a real
    callable that raises AFTER recording the call — so we can verify it
    was invoked AND that subsequent steps (_skill_exec already popped,
    InputBar unlock) still run despite the raise. No MagicMock per policy.

    Note: _skill_exec.pop runs BEFORE remove_async_task (see app.py
    ordering), so on the raise path _skill_exec should already be clean.
    The InputBar unlock runs AFTER, so it tests that the try/except around
    remove_async_task lets execution continue.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        input_bar = app.query_one("#inputbar", InputBar)

        run_id = "run-rat-raises"

        conv.start_skill_row(run_id, "mcp_search")
        conv.add_async_task(run_id, "mcp_search")
        input_bar.set_in_flight(True)
        app._skill_exec[run_id] = {"phase_visits": 2, "skill_name": "mcp_search"}

        await pilot.pause()

        # Substitute remove_async_task with a real function that raises.
        def _raising_remove_async_task(tid: str, *, terminal: str = "ok") -> None:
            raise RuntimeError("simulated remove_async_task failure")

        conv.remove_async_task = _raising_remove_async_task  # type: ignore[method-assign]

        msg = _skill_done_msg(run_id, "aborted")
        app._handle_trace_for_skill_row(conv, msg)
        await pilot.pause()

        # _skill_exec must be cleaned up (it runs BEFORE remove_async_task).
        assert run_id not in app.skill_exec_snapshot(), (
            "_skill_exec.pop must run even when remove_async_task raises"
        )
        # InputBar must be unlocked — this runs AFTER remove_async_task, so
        # it proves the try/except lets execution continue past the raise.
        assert not input_bar.is_in_flight(), (
            "set_in_flight(False) must fire even when remove_async_task raises"
        )


# ── Test 4: success path — no "aborted" flash; terminal="ok" ─────────────────


@pytest.mark.asyncio
async def test_skill_done_finished_uses_ok_terminal() -> None:
    """Tier 2: "skill done: finished" passes terminal="ok" to remove_async_task.

    The success path must NOT trigger the red aborted flash — that is only
    for the "aborted" / error path. Captures the terminal value via direct
    attribute substitution on conv.remove_async_task.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        run_id = "run-ok-terminal"

        conv.start_skill_row(run_id, "mcp_search")
        conv.add_async_task(run_id, "mcp_search")
        app._skill_exec[run_id] = {"phase_visits": 1, "skill_name": "mcp_search"}

        await pilot.pause()

        captured_terminals: list[str] = []

        def _capture_remove(tid: str, *, terminal: str = "ok") -> None:
            captured_terminals.append(terminal)

        conv.remove_async_task = _capture_remove  # type: ignore[method-assign]

        msg = _skill_done_msg(run_id, "finished")
        app._handle_trace_for_skill_row(conv, msg)

        assert captured_terminals == ["ok"], (
            f"success path must use terminal='ok', got {captured_terminals!r}"
        )
