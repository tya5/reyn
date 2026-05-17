"""Tier 2: /plan slash command (ADR-0023 Phase 2.1).

Two sub-commands:
  /plan list                 — show active plan runs
  /plan discard <plan_id>    — abort a specific plan run + cleanup

Mirrors test_skill_slash_command.py shape; uses real ChatSession +
SnapshotJournal so the WAL/snapshot paths exercise the production
wiring.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _drain_outbox(session: ChatSession) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


# ── /plan list ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_list_with_no_active_runs(tmp_path, monkeypatch):
    """Tier 2: /plan list with no active plans reports the empty hint."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/plan list")
    assert consumed is True
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    assert "no active plans" in combined


@pytest.mark.asyncio
async def test_plan_list_shows_running_plans(tmp_path, monkeypatch):
    """Tier 2: /plan list shows plan_ids from running_plans dict."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Simulate a running plan task by inserting into running_plans dict.
    # Use a never-completing future as the task placeholder so list shows
    # status="running".
    fut = asyncio.get_running_loop().create_future()
    session.running_plans["plan_abc123"] = fut

    try:
        consumed = await session._maybe_handle_slash("/plan list")
        assert consumed is True
        msgs = _drain_outbox(session)
        combined = "\n".join(m.text for m in msgs if m.kind == "system")
        assert "plan_abc123" in combined
        assert "running" in combined
    finally:
        fut.cancel()


@pytest.mark.asyncio
async def test_plan_list_shows_active_ids_without_task(tmp_path, monkeypatch):
    """Tier 2: plan_id in active_plan_ids but no running task (= post-crash
    pre-resume window) → list shows it as 'active (no task — resume pending)'."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Simulate a plan_id surviving in the agent snapshot without a task.
    await session._journal.record_plan_started(
        plan_id="plan_xyz789", goal="g", n_steps=2,
    )
    consumed = await session._maybe_handle_slash("/plan list")
    assert consumed is True
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs if m.kind == "system")
    assert "plan_xyz789" in combined
    assert "active" in combined or "resume pending" in combined


# ── /plan discard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_discard_unknown_id_reports_error(tmp_path, monkeypatch):
    """Tier 2: discarding a non-existent plan returns an error message."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/plan discard nonexistent")
    assert consumed is True
    msgs = _drain_outbox(session)
    err_msgs = [m for m in msgs if m.kind == "error"]
    assert len(err_msgs) >= 1
    assert "unknown plan run" in err_msgs[0].text


@pytest.mark.asyncio
async def test_plan_discard_records_plan_aborted(tmp_path, monkeypatch):
    """Tier 2: /plan discard emits plan_aborted to WAL + clears
    active_plan_ids."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._journal.record_plan_started(
        plan_id="p_to_discard", goal="g", n_steps=2,
    )
    assert "p_to_discard" in session._journal.snapshot.active_plan_ids

    consumed = await session._maybe_handle_slash("/plan discard p_to_discard")
    assert consumed is True

    # active_plan_ids cleared via plan_aborted apply.
    assert "p_to_discard" not in session._journal.snapshot.active_plan_ids

    # Confirmation message in outbox.
    msgs = _drain_outbox(session)
    status_texts = [m.text for m in msgs if m.kind == "system"]
    assert any("discarded plan run" in t for t in status_texts)


@pytest.mark.asyncio
async def test_plan_discard_cancels_running_task(tmp_path, monkeypatch):
    """Tier 2: discarding a plan with a running task cancels the task."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # A task we can cancel.
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _task_body():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_task_body())
    await started.wait()
    session.running_plans["p_running"] = task
    await session._journal.record_plan_started(
        plan_id="p_running", goal="g", n_steps=1,
    )

    consumed = await session._maybe_handle_slash("/plan discard p_running")
    assert consumed is True
    assert task.cancelled() or cancelled.is_set()
    assert "p_running" not in session.running_plans


@pytest.mark.asyncio
async def test_plan_discard_deletes_decomposition_artifact(tmp_path, monkeypatch):
    """Tier 2: discard removes the decomposition artifact via
    delete_plan_decomposition (= P5 cleanup)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Pre-create an artifact file at the production path.
    from reyn.chat.planner import Plan, PlanStep
    from reyn.plan.decomposition import (
        decomposition_path,
        write_decomposition,
    )
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    plan = Plan(
        goal="g",
        steps=(PlanStep("s1", "first", ()), PlanStep("s2", "second", ())),
    )
    write_decomposition(agent_state_dir, "p_artifact", plan)
    artifact = decomposition_path(agent_state_dir, "p_artifact")
    assert artifact.exists()

    await session._journal.record_plan_started(
        plan_id="p_artifact", goal="g", n_steps=2,
    )

    await session._maybe_handle_slash("/plan discard p_artifact")
    assert not artifact.exists()


# ── Usage / unknown sub-command ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_no_subcommand_shows_usage(tmp_path, monkeypatch):
    """Tier 2: bare `/plan` shows usage hint."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/plan")
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    assert "Usage:" in combined or "list" in combined


# ── /plan resume --from <step_id> ────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_resume_missing_args_shows_usage(tmp_path, monkeypatch):
    """Tier 2: /plan resume without --from <step_id> → usage error."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/plan resume")
    msgs = _drain_outbox(session)
    err = [m for m in msgs if m.kind == "error"]
    assert len(err) >= 1
    assert "Usage" in err[0].text or "--from" in err[0].text


@pytest.mark.asyncio
async def test_plan_resume_unknown_plan_id_reports_error(tmp_path, monkeypatch):
    """Tier 2: /plan resume nonexistent --from s1 → unknown plan error."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/plan resume nonexistent --from s1")
    msgs = _drain_outbox(session)
    err = [m for m in msgs if m.kind == "error"]
    assert len(err) >= 1
    assert "unknown plan run" in err[0].text


@pytest.mark.asyncio
async def test_plan_resume_missing_artifact_reports_error(tmp_path, monkeypatch):
    """Tier 2: /plan resume on a plan whose decomposition artifact is
    missing → descriptive error directing to /plan discard."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Set up a per-plan snapshot WITHOUT writing the decomposition artifact.
    from pathlib import Path

    from reyn.plan import PlanRegistry
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    reg = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg.start(plan_id="p_no_art", chain_id="c1", goal="g", applied_seq=10)

    await session._maybe_handle_slash("/plan resume p_no_art --from s1")
    msgs = _drain_outbox(session)
    err = [m for m in msgs if m.kind == "error"]
    assert len(err) >= 1
    assert (
        "decomposition" in err[0].text or "discard" in err[0].text
    )


@pytest.mark.asyncio
async def test_plan_resume_unknown_step_reports_error(tmp_path, monkeypatch):
    """Tier 2: /plan resume with --from pointing at a step that's not in
    the decomposition → error listing valid step IDs."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    from pathlib import Path

    from reyn.chat.planner import Plan, PlanStep
    from reyn.plan import PlanRegistry, write_decomposition
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    plan = Plan(goal="g", steps=(
        PlanStep("s1", "first", ()),
        PlanStep("s2", "second", ()),
    ))
    write_decomposition(agent_state_dir, "p001", plan)
    reg = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)

    await session._maybe_handle_slash("/plan resume p001 --from nonexistent")
    msgs = _drain_outbox(session)
    err = [m for m in msgs if m.kind == "error"]
    assert len(err) >= 1
    assert "not in plan" in err[0].text or "nonexistent" in err[0].text


@pytest.mark.asyncio
async def test_plan_resume_clears_target_step_results(tmp_path, monkeypatch):
    """Tier 2: /plan resume clears results from --from onward and reports
    the count of steps to re-execute."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    from pathlib import Path

    from reyn.chat.planner import Plan, PlanStep
    from reyn.plan import PlanRegistry, write_decomposition
    agent_state_dir = (
        Path(".reyn") / "agents" / session.agent_name / "state"
    )
    plan = Plan(goal="g", steps=(
        PlanStep("s1", "first", ()),
        PlanStep("s2", "second", ()),
        PlanStep("s3", "third", ()),
    ))
    write_decomposition(agent_state_dir, "p001", plan)

    # Pre-populate snapshot with results for all 3 steps.
    reg = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text="r1",
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s2", applied_seq=20, result_text="r2",
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s3", applied_seq=25, result_text="r3",
    )

    await session._maybe_handle_slash("/plan resume p001 --from s2")

    # Status confirmation in outbox.
    msgs = _drain_outbox(session)
    status_texts = [m.text for m in msgs if m.kind == "system"]
    assert any("resumed from step" in t for t in status_texts)

    # Reload registry and confirm s1 preserved, s2/s3 cleared.
    reg2 = PlanRegistry(
        agent_name=session.agent_name, agent_state_dir=agent_state_dir,
    )
    reg2.load_active()
    snap = reg2.get("p001")
    assert snap.step_results == {"s1": "r1"}
    assert snap.last_committed_step_id == "s1"


@pytest.mark.asyncio
async def test_plan_unknown_subcommand_reports_error(tmp_path, monkeypatch):
    """Tier 2: unknown sub-command surfaces an error message."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/plan nonsense")
    msgs = _drain_outbox(session)
    err = [m for m in msgs if m.kind == "error"]
    assert len(err) >= 1
