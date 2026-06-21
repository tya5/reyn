"""Tier 2: #1953 slice 7 — TaskWaker wake-triple against REAL AgentRegistry + Session.

Complements ``test_task_waker_slice7_1953.py`` (which drives the waker with
recording ``_RecRegistry`` / ``_RecSession`` to pin the call SHAPE). This pins
that the SAME driver works against the PRODUCTION ``AgentRegistry`` + ``Session``
construction:

  - ``resolve_session`` returns a real ``Session`` (deterministic routing-key);
  - ``_put_inbox`` lands the OS message on that session's real asyncio inbox;
  - ``ensure_session_running`` actually boots a run-loop task (observed via the
    registry's public ``running_tasks()``).

A recording-fake unit cannot catch a real-construction regression (a resolve-key
drift, an inbox-attribute rename, an ensure_session_running that silently no-ops
against the real registry); this can. No mocks — real components throughout
(the run-loop is booted then cancelled before it executes, so no LLM is needed:
the thing under test is the OS wake-driver, not the agent's recovery turn).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.task_wake import (
    WAKE_PARENT_KIND,
    WAKE_READY_KIND,
    TaskWaker,
)
from reyn.runtime.session import Session
from reyn.task import TaskState


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry + on-disk agent + real Session factory (no mocks)."""
    state_log = StateLog(tmp_path / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _cancel_running(reg: AgentRegistry) -> None:
    """Cancel the booted run-loop task(s) BEFORE they execute (no LLM turn)."""
    tasks = reg.running_tasks()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_notify_parent_decide_real_wake_triple(tmp_path):
    """Tier 2: abort-side wake against the REAL registry+session — resolve yields a
    real (deterministic) Session, the disposition lands on its real inbox, and a
    run-loop boots."""
    reg = _make_registry(tmp_path)
    waker = TaskWaker(reg, "alice")
    terminal = SimpleNamespace(task_id="B", name="do-B", status=TaskState.ABORTED)
    deps = [SimpleNamespace(task_id="A")]
    before = len(reg.running_tasks())
    try:
        await waker.notify_parent_decide(
            parent_session="a2a:ctx-parent", terminal_task=terminal, dependents=deps)

        # real resolve — deterministic: same routing-key → same Session object
        parent = reg.resolve_session("alice", "a2a", "ctx-parent")
        assert parent is reg.resolve_session("alice", "a2a", "ctx-parent")
        # real boot — ensure_session_running started a run-loop task
        assert len(reg.running_tasks()) > before
        # real delivery — the OS message is on the session's own asyncio inbox
        assert parent.inbox.qsize() >= 1
        kind, payload = parent.inbox.get_nowait()
        assert kind == WAKE_PARENT_KIND
        assert "B" in payload["text"] and "stuck" in payload["text"]
    finally:
        await _cancel_running(reg)


@pytest.mark.asyncio
async def test_wake_ready_dependent_real_wake_triple(tmp_path):
    """Tier 2: ready-side wake against the REAL registry+session — the dependent's
    sibling session is resolved, receives the task_ready message, and boots."""
    reg = _make_registry(tmp_path)
    waker = TaskWaker(reg, "alice")
    ready = SimpleNamespace(task_id="A", name="do-A", assignee="a2a:ctx-A",
                            status=TaskState.READY)
    before = len(reg.running_tasks())
    try:
        await waker.wake_ready_dependent(ready)

        depA = reg.resolve_session("alice", "a2a", "ctx-A")
        assert depA is reg.resolve_session("alice", "a2a", "ctx-A")
        assert len(reg.running_tasks()) > before
        assert depA.inbox.qsize() >= 1
        kind, payload = depA.inbox.get_nowait()
        assert kind == WAKE_READY_KIND
        assert "A" in payload["text"] and "READY" in payload["text"]
    finally:
        await _cancel_running(reg)


@pytest.mark.asyncio
async def test_wakes_resolve_distinct_sibling_sessions(tmp_path):
    """Tier 2: distinct routing-keys of the SAME agent resolve to DISTINCT sibling
    sessions — the woken parent (abort-side) and dependent (ready-side) are not the
    same Session (single-agent scope, sibling-session wake)."""
    reg = _make_registry(tmp_path)
    waker = TaskWaker(reg, "alice")
    terminal = SimpleNamespace(task_id="B", name="do-B", status=TaskState.ABORTED)
    ready = SimpleNamespace(task_id="A", name="do-A", assignee="a2a:ctx-A",
                            status=TaskState.READY)
    try:
        await waker.notify_parent_decide(
            parent_session="a2a:ctx-parent", terminal_task=terminal,
            dependents=[SimpleNamespace(task_id="A")])
        await waker.wake_ready_dependent(ready)

        parent = reg.resolve_session("alice", "a2a", "ctx-parent")
        depA = reg.resolve_session("alice", "a2a", "ctx-A")
        assert parent is not depA
    finally:
        await _cancel_running(reg)
