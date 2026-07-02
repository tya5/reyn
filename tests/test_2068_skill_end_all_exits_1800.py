"""Tier 2: #2068 — skill_end fires on EVERY exit, exactly-once per run-segment.

skill_start fires per (re)entry to SkillRegistry.start() (incl. resume); the symmetric
skill_end now fires on EVERY exit — completion (complete()) AND interrupt/error
(interrupt()) — guarded exactly-once per run-SEGMENT (re-armed by start()). The interrupt
path fires the HOOK ONLY (no skill_completed WAL, no snapshot unlink — will_resume). A
/skill discard pre-fires skill_end("discarded") + arms the guard before cancelling, so the
cancel-unwind's interrupt() defers and the hook gets the correct "discarded" status.

Real SkillRegistry + a recording HookDispatcher (the injected seam; no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.skill.skill_registry import SkillRegistry


class _RecordingDispatcher:
    """A real recording HookDispatcher stand-in — records each dispatch(point, vars)."""

    def __init__(self) -> None:
        self.dispatched: "list[tuple[str, dict]]" = []

    async def dispatch(self, point: str, template_vars: dict) -> None:
        self.dispatched.append((point, template_vars))

    @property
    def points(self) -> "list[str]":
        return [p for (p, _v) in self.dispatched]

    def statuses(self) -> "list[str]":
        return [v.get("status") for (p, v) in self.dispatched if p == "skill_end"]


def _registry(tmp_path: Path, rec: _RecordingDispatcher) -> SkillRegistry:
    state_dir = tmp_path / ".reyn" / "agents" / "a" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return SkillRegistry(
        agent_name="a", agent_state_dir=state_dir, state_log=None, hook_dispatcher=rec,
    )


def _snap_path(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / ".reyn" / "agents" / "a" / "state" / "skills" / f"{run_id}.snapshot.json"


@pytest.mark.asyncio
async def test_interrupt_fires_skill_end_interrupted(tmp_path):
    """Tier 2: (LOAD-BEARING) interrupt() fires skill_end with status="interrupted" — the
    path that previously bypassed complete() and never fired skill_end. RED before the fix
    (skill_start with no matching skill_end)."""
    rec = _RecordingDispatcher()
    reg = _registry(tmp_path, rec)
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    await reg.interrupt(run_id="r1")
    assert rec.points == ["skill_start", "skill_end"]
    assert rec.statuses() == ["interrupted"]


@pytest.mark.asyncio
async def test_interrupt_does_not_unlink_snapshot(tmp_path):
    """Tier 2: (the complete()/interrupt() asymmetry) interrupt() preserves the snapshot
    (will_resume), unlike complete() which removes it. RED if interrupt() teardown the
    snapshot (resume would lose it)."""
    rec = _RecordingDispatcher()
    reg = _registry(tmp_path, rec)
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    assert _snap_path(tmp_path, "r1").exists()  # start created it
    await reg.interrupt(run_id="r1")
    assert _snap_path(tmp_path, "r1").exists(), "interrupt() must preserve the snapshot"


@pytest.mark.asyncio
async def test_discard_pre_fire_then_interrupt_is_one_skill_end_discarded(tmp_path):
    """Tier 2: (LOAD-BEARING, the Q2 fix) /skill discard pre-fires skill_end("discarded")
    + arms the guard; the ensuing cancel-unwind's interrupt() then DEFERS. The hook fires
    EXACTLY ONCE with status="discarded" (NOT "interrupted"). RED if the order/guard lets
    interrupt() win the status (count-only would miss the wrong status)."""
    rec = _RecordingDispatcher()
    reg = _registry(tmp_path, rec)
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    await reg.mark_skill_ended("r1", "discarded")   # the discard pre-fire (before cancel)
    await reg.interrupt(run_id="r1")                 # the cancel-unwind → must DEFER
    assert rec.points == ["skill_start", "skill_end"]   # exactly one skill_end
    assert rec.statuses() == ["discarded"]              # the user-disposition wins, not "interrupted"


@pytest.mark.asyncio
async def test_complete_after_interrupt_is_suppressed(tmp_path):
    """Tier 2: (the guard, defense-in-depth) once skill_end fired this segment, a later
    complete() does NOT re-dispatch it — exactly-once. (complete()'s WAL/teardown still
    run; only the hook is guarded.)"""
    rec = _RecordingDispatcher()
    reg = _registry(tmp_path, rec)
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    await reg.interrupt(run_id="r1")                 # fires skill_end(interrupted)
    await reg.complete(run_id="r1", status="completed")  # skill_end suppressed
    assert rec.points == ["skill_start", "skill_end"]
    assert rec.statuses() == ["interrupted"]


@pytest.mark.asyncio
async def test_start_re_arms_guard_per_segment(tmp_path):
    """Tier 2: (the per-SEGMENT re-arm, lead's add) a RESUME enters start() again → a fresh
    skill_start + skill_end pair fires for the second segment (skill_end is once-per-segment,
    not once-per-run_id-lifetime). RED if start() doesn't re-arm the guard (the 2nd skill_end
    would be suppressed)."""
    rec = _RecordingDispatcher()
    reg = _registry(tmp_path, rec)
    # segment 1: enter → complete
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    await reg.complete(run_id="r1", status="completed")
    # segment 2: RE-ENTER (resume) → complete again
    await reg.start(run_id="r1", skill_name="s", skill_input={})
    await reg.complete(run_id="r1", status="completed")
    assert rec.points == ["skill_start", "skill_end", "skill_start", "skill_end"]
