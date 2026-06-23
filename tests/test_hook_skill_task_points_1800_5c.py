"""Tier 2: #1800 slice 5c — skill/task lifecycle hook points + live threading.

5b wired the 4 session/turn points. 5c wires the remaining 4 — skill_start/end,
task_start/end — at the async execution points (SkillRegistry.start/complete;
op_runtime/task.py _create / _update_status→COMPLETED / _abort). Those points are
NOT in Session, so the Session's HookDispatcher is THREADED into them (SkillRegistry
ctor; OpContext via the shared build_router_op_context + the kernel chain).

The construction-forwarding axis (lead): the dispatch must fire on the REAL
execution path with the LIVE instance — not a None no-op. So these tests run the
actual SkillRegistry/task-op code with a threaded recording dispatcher (the real
path fires it), and prove the Session forwards its live instance into the router
OpContext (both callers).

No MagicMock: real SkillRegistry / InMemoryTaskBackend / Session + a recording
dispatcher (a real instance, the injected seam). Tier declared; unpack idiom.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime import task as taskmod
from reyn.runtime.session import Session
from reyn.skill.skill_registry import SkillRegistry
from reyn.task import InMemoryTaskBackend


class _RecordingDispatcher:
    """A real recording HookDispatcher stand-in (the injected seam) — records each
    dispatch(point, vars)."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict]] = []

    async def dispatch(self, point: str, template_vars: dict) -> None:
        self.dispatched.append((point, template_vars))

    @property
    def points(self) -> list[str]:
        return [p for (p, _v) in self.dispatched]


def _task_ctx(backend, dispatcher, *, session: str = "sess") -> SimpleNamespace:
    return SimpleNamespace(
        task_backend=backend, session_id=session, agent_id="a", events=None,
        task_waker=None, threat_scan=None, hook_dispatcher=dispatcher,
    )


# --- skill points (real SkillRegistry) --------------------------------------


@pytest.mark.asyncio
async def test_skill_registry_start_and_complete_fire_points(tmp_path):
    """Tier 2: a real SkillRegistry.start fires skill_start and .complete fires
    skill_end via the threaded dispatcher (the real execution path)."""
    rec = _RecordingDispatcher()
    reg = SkillRegistry(
        agent_name="a", agent_state_dir=tmp_path, state_log=None, hook_dispatcher=rec,
    )

    await reg.start(run_id="r1", skill_name="my_skill", skill_input={})
    await reg.complete(run_id="r1")

    assert rec.points == ["skill_start", "skill_end"]
    # ctx carries the run/skill identity for templates
    (_p0, start_vars), (_p1, end_vars) = rec.dispatched
    assert start_vars["run_id"] == "r1" and start_vars["skill_name"] == "my_skill"
    assert end_vars["run_id"] == "r1" and end_vars["status"] == "completed"


# --- task points (real op_runtime/task.py via OpContext) --------------------


@pytest.mark.asyncio
async def test_task_create_fires_task_start():
    """Tier 2: a real _create fires task_start via ctx.hook_dispatcher."""
    rec = _RecordingDispatcher()
    ctx = _task_ctx(InMemoryTaskBackend(), rec)
    await taskmod._create(
        SimpleNamespace(name="t", assignee="sess", requester="sess",
                        origin="self", description="d", deps=[]),
        ctx, "control_ir",
    )
    assert "task_start" in rec.points


@pytest.mark.asyncio
async def test_task_update_status_completed_fires_task_end():
    """Tier 2: a real _update_status → COMPLETED fires task_end (status completed)."""
    rec = _RecordingDispatcher()
    backend = InMemoryTaskBackend()
    ctx = _task_ctx(backend, rec)
    created = await taskmod._create(
        SimpleNamespace(name="t", assignee="sess", requester="sess",
                        origin="self", description="d", deps=[]),
        ctx, "control_ir",
    )
    tid = created["task"]["task_id"]

    await taskmod._update_status(
        SimpleNamespace(task_id=tid, status="completed"), ctx, "control_ir")

    end = [(p, v) for (p, v) in rec.dispatched if p == "task_end"]
    assert end and end[-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_task_abort_fires_task_end_aborted():
    """Tier 2: a real _abort fires task_end with status=aborted (symmetric with
    create→task_start, for every started task in the aborted sub-tree)."""
    rec = _RecordingDispatcher()
    backend = InMemoryTaskBackend()
    ctx = _task_ctx(backend, rec)
    created = await taskmod._create(
        SimpleNamespace(name="t", assignee="sess", requester="sess",
                        origin="self", description="d", deps=[]),
        ctx, "control_ir",
    )
    tid = created["task"]["task_id"]

    await taskmod._abort(SimpleNamespace(task_id=tid, reason="done"), ctx, "control_ir")

    aborted_ends = [v for (p, v) in rec.dispatched if p == "task_end" and v["status"] == "aborted"]
    assert any(v["task_id"] == tid for v in aborted_ends)


@pytest.mark.asyncio
async def test_no_dispatcher_is_noop():
    """Tier 2: a None dispatcher on the ctx → the task op runs, no dispatch, no
    error (the no-op equivalence at the dispatch sites)."""
    ctx = _task_ctx(InMemoryTaskBackend(), None)
    res = await taskmod._create(
        SimpleNamespace(name="t", assignee="sess", requester="sess",
                        origin="self", description="d", deps=[]),
        ctx, "control_ir",
    )
    assert res["status"] == "ok"


# --- live threading: the Session forwards its instance into the router OpContext


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="fwd-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def test_session_router_op_contexts_carry_the_live_dispatcher(tmp_path):
    """Tier 2: construction-forwarding — BOTH router-OpContext callers forward the
    Session's LIVE dispatcher instance (not None) — the Session-direct
    _make_router_op_context AND the RouterHostAdapter — so task ops on either flow
    can fire the hooks."""
    from reyn.hooks.dispatcher import HookDispatcher

    session = _make_session(tmp_path)

    direct = session._make_router_op_context()
    via_adapter = session._router_host.make_router_op_context()

    # both callers forward a real, LIVE HookDispatcher (not a None no-op)…
    assert isinstance(direct.hook_dispatcher, HookDispatcher)
    # …and it is the SAME single instance on both router flows (no flow misses it).
    assert direct.hook_dispatcher is via_adapter.hook_dispatcher
