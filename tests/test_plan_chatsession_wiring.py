"""Tier 2: Phase 2 fresh-run wiring (= ADR-0023 Phase 2 v1 gap fix).

Pins the contract that ChatSession's record_plan_* methods populate
the per-agent PlanRegistry alongside SnapshotJournal's WAL-side
bookkeeping. Without this, ADR-0023 forward replay was dormant
(PlanRegistry.load_active() returns empty for every fresh-run plan)
and ADR-0024/ADR-0025 features had nothing to record into.

Tests target the public surface (ChatSession.record_plan_* methods)
and observe via PlanRegistry's on-disk snapshots — no private state
assertions, no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog
from reyn.plan import (
    PlanRegistry,
    plan_snapshot_path,
)


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


# ── per-plan snapshot creation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_plan_started_creates_per_plan_snapshot(tmp_path, monkeypatch):
    """Tier 2: RouterHostAdapter.record_plan_started (via session.router_host) writes the per-plan
    snapshot file alongside the WAL append + AgentSnapshot mutation."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    await session.router_host.record_plan_started(
        plan_id="p_test", goal="hello", n_steps=2,
    )

    # Per-plan snapshot file exists at the documented path.
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    snap_path = plan_snapshot_path(agent_state_dir, "p_test")
    assert snap_path.exists()

    # Registry can load it back.
    reg = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg.load_active()
    snap = reg.get("p_test")
    assert snap is not None
    assert snap.goal == "hello"
    assert snap.chain_id == "plan_p_test"  # ADR-0023 §2.1.2 per-plan chain


@pytest.mark.asyncio
async def test_record_plan_step_completed_persists_to_snapshot(tmp_path, monkeypatch):
    """Tier 2: RouterHostAdapter.record_plan_step_completed flows result_text
    through to PlanRegistry.record_step_completed (= ADR-0024 inline
    or spilled persistence in the per-plan snapshot)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    await session.router_host.record_plan_started(
        plan_id="p_test", goal="g", n_steps=1,
    )
    await session.router_host.record_plan_step_started(
        plan_id="p_test", step_id="s1", depends_on=[], n_tools=0,
    )

    text = "step output text"
    await session.router_host.record_plan_step_completed(
        plan_id="p_test", step_id="s1",
        content_len=len(text), result_text=text,
    )

    # Re-load the snapshot.
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    reg = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg.load_active()
    snap = reg.get("p_test")
    assert snap.step_results["s1"] == text
    assert snap.last_committed_step_id == "s1"


@pytest.mark.asyncio
async def test_record_plan_step_completed_spills_large_text(tmp_path, monkeypatch):
    """Tier 2: large result_text routes through ADR-0024 spill path so
    fresh runs benefit from the spill (= no 32KB silent truncation)."""
    from reyn.plan import step_result_file_path

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    await session.router_host.record_plan_started(
        plan_id="p_big", goal="g", n_steps=1,
    )
    huge = "X" * 50_000
    await session.router_host.record_plan_step_completed(
        plan_id="p_big", step_id="s1",
        content_len=len(huge), result_text=huge,
    )

    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    spilled = step_result_file_path(agent_state_dir, "p_big", "s1")
    assert spilled.exists()
    assert spilled.read_text(encoding="utf-8") == huge


@pytest.mark.asyncio
async def test_record_plan_completed_removes_per_plan_workspace(tmp_path, monkeypatch):
    """Tier 2: RouterHostAdapter.record_plan_completed reclaims the per-plan
    workspace via PlanRegistry.complete (= delete_plan_workspace)."""
    from reyn.plan import decomposition_dir

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    await session.router_host.record_plan_started(
        plan_id="p_done", goal="g", n_steps=1,
    )
    await session.router_host.record_plan_step_completed(
        plan_id="p_done", step_id="s1", content_len=5, result_text="hello",
    )

    await session.router_host.record_plan_completed(plan_id="p_done")

    # Per-plan workspace dir + snapshot file gone.
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    assert not decomposition_dir(agent_state_dir, "p_done").exists()
    assert not plan_snapshot_path(agent_state_dir, "p_done").exists()


@pytest.mark.asyncio
async def test_record_plan_aborted_removes_per_plan_workspace(tmp_path, monkeypatch):
    """Tier 2: RouterHostAdapter.record_plan_aborted similarly cleans up the
    per-plan workspace (= /plan discard / restart-cleanup paths)."""
    from reyn.plan import decomposition_dir

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    await session.router_host.record_plan_started(
        plan_id="p_abort", goal="g", n_steps=1,
    )
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    assert plan_snapshot_path(agent_state_dir, "p_abort").exists()

    await session.router_host.record_plan_aborted(
        plan_id="p_abort", reason="test",
    )
    assert not plan_snapshot_path(agent_state_dir, "p_abort").exists()


# ── PlanRegistry sharing ─────────────────────────────────────────────────


def test_get_plan_registry_returns_singleton(tmp_path, monkeypatch):
    """Tier 2: lazy-init returns the same instance across calls."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    reg1 = session._get_plan_registry()
    reg2 = session._get_plan_registry()
    assert reg1 is reg2


def test_get_plan_registry_returns_none_without_state_log(tmp_path):
    """Tier 2: no state_log → no plan registry (= test/standalone mode)."""
    session = ChatSession(
        agent_name="alpha", state_log=None,
        snapshot_path=tmp_path / "snap.json",
    )
    assert session._get_plan_registry() is None


# ── FP-0031-A: plan summary status before execution ──────────────────────


class _StubRuntime:
    """Minimal PlanRuntime stub for spawn_plan_task tests.

    Exposes the `plan` attribute (required by FP-0031-A) but never
    actually executes (run() is not called in this test). The task
    created by spawn_plan_task is cancelled before it can fire.
    """

    def __init__(self, plan):
        self.plan = plan

    async def run(self):
        # Placeholder — never called in Component A tests.
        from reyn.chat.planner import PlanExecutionResult
        return PlanExecutionResult(text="")


@pytest.mark.asyncio
async def test_spawn_plan_task_emits_plan_summary_before_execution(
    tmp_path, monkeypatch
):
    """Tier 2: FP-0031-A — spawn_plan_task emits a plan_summary status
    message to the outbox *before* the background task starts executing.

    Observable contract: after awaiting spawn_plan_task(), at least one
    status message with source="plan_summary" is in session.outbox; the
    text contains each step description in numbered order.
    """
    import asyncio

    from reyn.chat.planner import Plan, PlanStep

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    # Attach so status messages are not dropped by _put_outbox.
    session.is_attached = True

    plan = Plan(
        goal="test plan summary",
        steps=(
            PlanStep(id="s1", description="gather data", tools=()),
            PlanStep(id="s2", description="synthesise results", tools=(), depends_on=("s1",)),
        ),
    )
    stub_runtime = _StubRuntime(plan)

    await session._plan_runner.spawn_plan_task(
        plan_id="test_summary", runtime=stub_runtime, chain_id="plan_test_summary",
    )

    # Collect all status messages with source="plan_summary" from the outbox.
    # The background task may or may not have run yet; we only check the
    # summary message emitted synchronously before task creation.
    summary_msgs = []
    while not session.outbox.empty():
        try:
            msg = session.outbox.get_nowait()
            if (
                msg.kind == "system"
                and msg.meta.get("source") == "plan_summary"
            ):
                summary_msgs.append(msg)
        except asyncio.QueueEmpty:
            break

    assert summary_msgs, "Expected at least one plan_summary status message before task execution"
    summary_text = summary_msgs[0].text
    assert "1. gather data" in summary_text
    assert "2. synthesise results" in summary_text
    assert summary_msgs[0].meta.get("plan_id") == "test_summary"

    # Cancel the background task to avoid asyncio warnings.
    task = session.running_plans.pop("test_summary", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
