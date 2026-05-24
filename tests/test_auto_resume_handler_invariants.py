"""Tier 2: OS invariant tests for AutoResumeHandler (FP-0019 Wave 3).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real StateLog, real AutoResumeHandler.
- Event observation via ``events.all()`` (EventLog public read accessor).
- WAL observation via ``StateLog.iter_from(0)`` (public read accessor).
- Each test docstring's first line starts with ``Tier 2: ...``.

Reference: FP-0019 Wave 3 (extracted from ChatSession._auto_resume_active_skills).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.services.auto_resume_handler import AutoResumeHandler
from reyn.events.events import EventLog
from reyn.events.state_log import StateLog
from reyn.skill.skill_resume_coordinator import ResumeDecision
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_skill_snapshot(
    *,
    tmp_path: Path,
    agent_name: str,
    run_id: str,
    skill_name: str = "demo",
    has_ambiguity: bool = False,
) -> None:
    """Pre-populate per-skill snapshot + (optional) ambiguous WAL events.

    Mimics on-disk state after a crash: a per-skill snapshot exists
    with current_phase set, and optionally a step_started without a
    matching step_completed (= ambiguous step).
    """
    state_dir = tmp_path / ".reyn" / "agents" / agent_name / "state"
    skills_dir = state_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
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
        log = StateLog(tmp_path / "state.wal")

        async def _emit() -> None:
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


def _make_handler(
    *,
    tmp_path: Path,
    agent_name: str = "alpha",
    launched: "list[ResumeDecision]",
) -> tuple[AutoResumeHandler, EventLog, StateLog]:
    """Construct an AutoResumeHandler with real collaborators.

    ``launched`` is a list that accumulates every decision passed to the
    injected launcher.  The SkillRegistry is constructed lazily inside the
    handler via ``get_skill_registry``; it derives its state dir from the
    chdir-adjusted cwd (set by the test's monkeypatch).
    """
    events = EventLog()
    wal = StateLog(tmp_path / "state.wal")

    from reyn.skill.skill_registry import SkillRegistry

    _registry: SkillRegistry | None = None

    def _get_registry() -> SkillRegistry | None:
        nonlocal _registry
        if _registry is None:
            agent_state_dir = (
                Path(".reyn") / "agents" / agent_name / "state"
            )
            _registry = SkillRegistry(
                agent_name=agent_name,
                agent_state_dir=agent_state_dir,
                state_log=wal,
                truncate_eligible_hook=None,
            )
        return _registry

    def _drop(run_id: str | None) -> None:
        pass

    async def _launcher(decision: ResumeDecision) -> None:
        launched.append(decision)

    handler = AutoResumeHandler(
        event_log=events,
        state_log=wal,
        get_skill_registry=_get_registry,
        drop_interventions_for_run=_drop,
        launcher=_launcher,
    )
    return handler, events, wal


# ---------------------------------------------------------------------------
# Invariant 1: empty WAL → resume_active() returns 0, no events emitted
# ---------------------------------------------------------------------------


def test_resume_with_empty_wal_returns_zero(tmp_path: Path, monkeypatch):
    """Tier 2: empty WAL → resume_active() = 0, no skill_run_resumed emitted.

    P6 invariant: when there are no in-flight skill_runs on disk, the
    handler must be a strict no-op — zero launches, zero events.
    """
    monkeypatch.chdir(tmp_path)
    launched: list[ResumeDecision] = []
    handler, events, _ = _make_handler(tmp_path=tmp_path, launched=launched)

    count = asyncio.run(handler.resume_active())

    assert count == 0, f"Expected 0 resumed, got {count}"
    assert launched == [], "No launcher calls expected on empty WAL"
    resumed_events = [e for e in events.all() if e.type == "skill_run_resumed"]
    assert resumed_events == [], (
        f"Expected no skill_run_resumed events, got {resumed_events}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: in-flight skill_run → skill_run_resumed emitted (P6)
# ---------------------------------------------------------------------------


def test_resume_active_emits_correct_events(tmp_path: Path, monkeypatch):
    """Tier 2: WAL has 1 in-flight skill_run → resume_active() emits skill_run_resumed.

    P6 invariant: every state change must produce an event.  The act of
    re-spawning a crashed skill_run is a state change; the corresponding
    ``skill_run_resumed`` event must be emitted via the injected event_log.
    """
    monkeypatch.chdir(tmp_path)
    _seed_skill_snapshot(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_p6_test", skill_name="demo",
    )
    launched: list[ResumeDecision] = []
    handler, events, _ = _make_handler(tmp_path=tmp_path, launched=launched)

    count = asyncio.run(handler.resume_active())

    assert count == 1, f"Expected 1 resumed, got {count}"
    resumed_events = [e for e in events.all() if e.type == "skill_run_resumed"]
    assert resumed_events, (
        "Expected at least one skill_run_resumed event, got none"
    )
    ev = resumed_events[0]
    assert not resumed_events[1:], (
        f"Expected exactly one skill_run_resumed event, got extras: {resumed_events[1:]}"
    )
    assert ev.data.get("run_id") == "run_p6_test", (
        f"skill_run_resumed.run_id must match seeded run_id, got {ev.data}"
    )
    assert ev.data.get("skill") == "demo", (
        f"skill_run_resumed.skill must match seeded skill_name, got {ev.data}"
    )


# ---------------------------------------------------------------------------
# Invariant 3: launcher is called with the correct ResumeDecision (SkillRunner contract)
# ---------------------------------------------------------------------------


def test_resume_skill_runner_dispatch_correct(tmp_path: Path, monkeypatch):
    """Tier 2: in-flight skill_run → launcher receives ResumeDecision with correct plan.

    Verifies the SkillRunner dispatch contract: the launcher callback must
    receive a ResumeDecision whose plan carries the original run_id and
    skill_name so the SkillRunner can reconstruct the Agent correctly.
    """
    monkeypatch.chdir(tmp_path)
    _seed_skill_snapshot(
        tmp_path=tmp_path, agent_name="alpha",
        run_id="run_dispatch_test", skill_name="my_skill",
    )
    launched: list[ResumeDecision] = []
    handler, _, _ = _make_handler(tmp_path=tmp_path, launched=launched)

    count = asyncio.run(handler.resume_active())

    assert count == 1, f"Expected 1 resumed, got {count}"
    assert launched, "Expected launcher to be called at least once"
    assert not launched[1:], f"Expected launcher called exactly once, got extras: {launched[1:]}"
    decision = launched[0]
    assert decision.plan.run_id == "run_dispatch_test", (
        f"Launcher must receive decision with run_id='run_dispatch_test', "
        f"got {decision.plan.run_id!r}"
    )
    assert decision.plan.skill_name == "my_skill", (
        f"Launcher must receive decision with skill_name='my_skill', "
        f"got {decision.plan.skill_name!r}"
    )
