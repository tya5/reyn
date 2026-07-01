"""Tier 2: /tasks kill <skill_run_id> must actually kill — not show a preview.

Root cause: _kill_task called _discard_skill_run(session, resolved) without
"--force".  _discard_skill_run without --force shows a confirmation preview and
returns without mutating state.  The fix passes ``f"{resolved} --force"`` so
/tasks kill bypasses the two-step confirmation that /skill discard (without
--force) requires — matching the semantics of a command literally named "kill".

Contrast with the dynamic-task path: /tasks kill on a dynamic task calls
backend.abort() immediately (no confirmation step); the skill path now matches.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.slash.tasks import _kill_task

# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeSnap:
    def __init__(self, skill_name: str, run_id: str) -> None:
        self.skill_name = skill_name
        self.skill_run_id = run_id


class _FakeRegistry:
    def __init__(self, snap: _FakeSnap) -> None:
        self._snap = snap
        self.mark_ended_calls: list[tuple[str, str]] = []
        self.complete_calls: list[tuple[str, str]] = []

    def get(self, run_id: str) -> _FakeSnap | None:
        return self._snap if self._snap.skill_run_id == run_id else None

    async def mark_skill_ended(self, run_id: str, status: str) -> None:
        self.mark_ended_calls.append((run_id, status))

    async def complete(self, *, run_id: str, status: str) -> None:
        self.complete_calls.append((run_id, status))


class _FakeSession:
    def __init__(self, run_id: str, snap: _FakeSnap, registry: _FakeRegistry) -> None:
        self._registry_obj = registry
        self.running_skills: dict = {}
        self.running_skills_started_at: dict = {run_id: 0.0}
        self.running_skills_chain: dict = {}
        self.replies: list[str] = []
        self._registry = None

        async def _never() -> None:
            await asyncio.sleep(9999)

        task = asyncio.ensure_future(_never())
        self.running_skills[run_id] = task

    def get_skill_registry(self) -> _FakeRegistry:
        return self._registry_obj

    def _drop_interventions_for_run(self, run_id: str) -> None:
        pass

    async def _put_outbox(self, message) -> None:
        self.replies.append(message.text)


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tasks_kill_skill_actually_kills_not_preview() -> None:
    """Tier 2: /tasks kill <skill_id> completes the discard — reply confirms
    'discarded', NOT the two-step preview 'Re-run with --force'."""
    run_id = "skill-run-abc12345"
    snap = _FakeSnap("my_skill", run_id)
    reg = _FakeRegistry(snap)
    session = _FakeSession(run_id, snap, reg)

    await _kill_task(session, "abc12345")  # prefix match

    assert session.replies, "no reply emitted"
    reply_text = session.replies[-1]
    # Pre-fix: this contained "Re-run with --force" (a preview, not a kill).
    assert "Re-run with --force" not in reply_text, (
        "/tasks kill should not show the two-step confirmation preview"
    )
    assert "discarded" in reply_text, (
        f"/tasks kill should confirm the kill; got: {reply_text!r}"
    )


@pytest.mark.asyncio
async def test_tasks_kill_skill_cancels_the_asyncio_task() -> None:
    """Tier 2: the asyncio.Task in running_skills is cancelled by /tasks kill."""
    run_id = "skill-run-xyz99999"
    snap = _FakeSnap("other_skill", run_id)
    reg = _FakeRegistry(snap)
    session = _FakeSession(run_id, snap, reg)

    raw_task = session.running_skills[run_id]
    assert not raw_task.done()

    await _kill_task(session, "xyz99999")

    # The task was cancelled and awaited — it should be done now.
    assert raw_task.done(), "asyncio.Task must be cancelled after /tasks kill"
