"""Tier 2: OS invariant — Session startup auto-resume hook.

After ``restore_state`` rehydrates the agent's snapshot + WAL, the
session must auto-launch resume tasks for every active skill run.
This is the headline UX of PR-resume-auto: no prompt, no manual
intervention — the user just sees their skills continue from where
they crashed.

The test pins:
  - ``_auto_resume_active_skills`` discovers active runs via
    SkillResumeCoordinator and applies the policy.
  - ``discard`` decisions trigger ``skill_discarded`` in the WAL and
    do NOT launch a task.
  - ``resume`` / ``retry`` / ``skip`` decisions are passed to the
    launcher (which production wires to a real SkillRuntime.run task).
  - Empty active list is a no-op (no false launches).

Reference: PR-resume-auto A2 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.skill.skill_resume_coordinator import (
    ResumeDecision,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _seed_active_run(
    *,
    tmp_path: Path,
    agent_name: str,
    run_id: str,
    skill_name: str = "demo",
    has_ambiguity: bool = False,
) -> None:
    """Pre-populate per-skill snapshot + (optional) ambiguous WAL events.

    Mimics the on-disk state after a crash: a per-skill snapshot exists
    with current_phase set, and (optionally) a step_started without
    matching step_completed for an ambiguous step.
    """
    state_dir = tmp_path / ".reyn" / "agents" / agent_name / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = state_dir / "skills"
    skills_dir.mkdir(exist_ok=True)
    from reyn.skill.skill_snapshot import SkillSnapshot
    snap = SkillSnapshot(
        skill_run_id=run_id,
        skill_name=skill_name,
        skill_input={"type": "input", "data": {}},
        applied_seq=10,
        last_phase_applied_seq=10,
        current_phase="draft",
        last_phase_artifact_path=None,
        history=["draft"],
        visit_counts={"draft": 1},
    )
    snap.save(skills_dir / f"{run_id}.snapshot.json")
    if has_ambiguity:
        # Append an orphan step_started so the analyzer sees ambiguity.
        log = StateLog(tmp_path / "state.wal")

        async def _emit():
            await log.append(
                "step_started",
                run_id=run_id,
                phase="draft",
                op_invocation_id="draft.0",
                op_kind="mcp",
                args={"server": "x"},
                args_hash="ab",
            )
        asyncio.run(_emit())


# ---------------------------------------------------------------------------
# Empty / no active runs
# ---------------------------------------------------------------------------


def test_auto_resume_empty_is_noop(tmp_path: Path):
    """Tier 2: no active skill_runs → no launches, no errors."""
    session = _make_session(tmp_path)
    launched: list[ResumeDecision] = []

    async def fake_launcher(d: ResumeDecision) -> None:
        launched.append(d)

    async def go():
        return await session._auto_resume_active_skills(
            launcher=fake_launcher,
        )

    decisions = asyncio.run(go())
    assert decisions == []
    assert launched == []


# ---------------------------------------------------------------------------
# Resume action launches
# ---------------------------------------------------------------------------


def test_auto_resume_clean_run_launches_task(tmp_path: Path, monkeypatch):
    """Tier 2: clean active run (no ambiguity) → launcher is called with action='resume'."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _seed_active_run(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_clean", skill_name="demo",
    )
    # SkillRegistry needs an agent_state_dir; the session's lazy
    # registry uses the agent_name to derive that path. To make
    # registry.load_active() see our seeded snapshot, ensure the
    # registry is constructed against the same path the seed used.

    launched: list[ResumeDecision] = []

    async def fake_launcher(d: ResumeDecision) -> None:
        launched.append(d)

    async def go():
        return await session._auto_resume_active_skills(
            launcher=fake_launcher,
        )

    asyncio.run(go())
    assert launched, "expected at least one launched resume"
    assert launched[0].action == "resume"
    assert launched[0].plan.run_id == "run_clean"
    assert launched[0].plan.skill_name == "demo"


# ---------------------------------------------------------------------------
# Discard policy → skill_discarded in WAL, no launch
# ---------------------------------------------------------------------------


def test_auto_resume_discards_skill_with_discard_policy(tmp_path: Path, monkeypatch):
    """Tier 2: ambiguous run + reyn.yaml policy=discard_skill → registry.complete + no launch.

    Verified end-to-end: WAL has skill_discarded, snapshot file
    removed, launcher NOT called for the discarded run.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _seed_active_run(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_disc", skill_name="demo",
        has_ambiguity=True,
    )

    launched: list[ResumeDecision] = []

    async def fake_launcher(d: ResumeDecision) -> None:
        launched.append(d)

    from reyn.config import SkillResumeConfig

    async def go():
        return await session._auto_resume_active_skills(
            launcher=fake_launcher,
            config=SkillResumeConfig(default="discard_skill"),
        )

    decisions = asyncio.run(go())
    # No remaining decisions returned (all discarded)
    assert decisions == []
    assert launched == []
    # WAL has skill_discarded
    log = StateLog(tmp_path / "state.wal")
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_discarded" in kinds
    # Snapshot file gone
    snap_path = (
        tmp_path / ".reyn" / "agents" / "alpha" / "state" / "skills"
        / "run_disc.snapshot.json"
    )
    assert not snap_path.exists()


def test_auto_resume_retry_default_launches_with_ambiguity(tmp_path: Path, monkeypatch):
    """Tier 2: ambiguous run + default policy=retry → launcher called with action='retry'.

    The auto-resume default treats ambiguous steps as "re-execute" —
    they're absent from committed_steps so dispatch_tool memo lookup
    misses → invoker runs fresh. The skill resumes; ambiguous step is
    not silently skipped or blocked on a prompt.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _seed_active_run(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_retry", skill_name="demo",
        has_ambiguity=True,
    )
    launched: list[ResumeDecision] = []

    async def fake_launcher(d: ResumeDecision) -> None:
        launched.append(d)

    async def go():
        return await session._auto_resume_active_skills(
            launcher=fake_launcher,
        )

    decisions = asyncio.run(go())
    assert decisions, "expected at least one resume decision"
    assert decisions[0].action == "retry"
    assert launched, "expected at least one launched task"
    assert launched[0].plan.run_id == "run_retry"


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_auto_resume_mixed_batch_separates_discard_from_launch(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: 2 runs (one clean, one ambiguous with per_skill discard) → 1 launch + 1 discard."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _seed_active_run(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_clean", skill_name="trustworthy_skill",
    )
    _seed_active_run(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_amb", skill_name="risky_skill",
        has_ambiguity=True,
    )

    launched: list[ResumeDecision] = []

    async def fake_launcher(d: ResumeDecision) -> None:
        launched.append(d)

    from reyn.config import SkillResumeConfig

    async def go():
        return await session._auto_resume_active_skills(
            launcher=fake_launcher,
            config=SkillResumeConfig(
                default="retry",
                per_skill={"risky_skill": "discard_skill"},
            ),
        )

    decisions = asyncio.run(go())
    # Only the clean run remains launchable
    assert decisions, "expected at least one resume decision"
    assert decisions[0].plan.run_id == "run_clean"
    # The launcher was called only for the launchable one
    assert launched, "expected at least one launched task"
    assert launched[0].plan.run_id == "run_clean"
    # The risky one was discarded (WAL event)
    log = StateLog(tmp_path / "state.wal")
    discarded_ids = [
        e["run_id"] for e in log.iter_from(0)
        if e["kind"] == "skill_discarded"
    ]
    assert discarded_ids == ["run_amb"]
