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
    """Tier 2: RouterHostAdapter.record_plan_started (via session._router_host) writes the per-plan
    snapshot file alongside the WAL append + AgentSnapshot mutation."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    await session._router_host.record_plan_started(
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
    await session._router_host.record_plan_started(
        plan_id="p_test", goal="g", n_steps=1,
    )
    await session._router_host.record_plan_step_started(
        plan_id="p_test", step_id="s1", depends_on=[], n_tools=0,
    )

    text = "step output text"
    await session._router_host.record_plan_step_completed(
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
    await session._router_host.record_plan_started(
        plan_id="p_big", goal="g", n_steps=1,
    )
    huge = "X" * 50_000
    await session._router_host.record_plan_step_completed(
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
    await session._router_host.record_plan_started(
        plan_id="p_done", goal="g", n_steps=1,
    )
    await session._router_host.record_plan_step_completed(
        plan_id="p_done", step_id="s1", content_len=5, result_text="hello",
    )

    await session._router_host.record_plan_completed(plan_id="p_done")

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
    await session._router_host.record_plan_started(
        plan_id="p_abort", goal="g", n_steps=1,
    )
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    assert plan_snapshot_path(agent_state_dir, "p_abort").exists()

    await session._router_host.record_plan_aborted(
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
