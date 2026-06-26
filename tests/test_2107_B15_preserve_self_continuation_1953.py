"""Tier 2: #2107 §16 B1.5 — a self-continuation preserves the execution context.

tui (live) found: while a session executes a task-as-request T, an iteration-cap
turn boundary → a HOOK self-continuation (#1800 slice-5b, kind="hook") re-enters
run_one_iteration; the old _stamp else-branch reset current_task_id=None → a sub-task
created on that continuation turn ORPHANED (requester=session), which §16 B2's
list(requester==T) ownership-cascade would then MISS.

B1.5 classifies EVERY trigger kind run_one_iteration dispatches (complete-by-
construction), in three bands:
  - SET (a task wake introduces the context): task_ready, task_dependency_aborted.
  - PRESERVE (a self-continuation / a response to the agent's OWN prior action — not a
    new context): hook, agent_response, skill_completed.
  - RESET→None (a genuinely NEW external context): user, agent_request (+ unknown,
    fail-safe to session-owned).
Interleaving-safe: only a task_ready switches tasks, so the PRESERVE bands cannot
leak T1 into a T2 turn.

Tests assert on the BUILT op-ctx (public builder output) / the created task, not
private state. The headline goes RED if _stamp resets on a hook self-continuation.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime import task as taskmod
from reyn.hooks.dispatcher import HOOK_INBOX_KIND
from reyn.runtime.services.task_wake import WAKE_READY_KIND
from reyn.runtime.session import Session
from reyn.task import InMemoryTaskBackend
from reyn.task.ref import is_task_ref, make_task_ref
from tests._support.router_host_adapter import make_adapter


def _create_op(name, *, deps=None):
    return SimpleNamespace(name=name, description=f"do {name}", deps=list(deps or []),
                           assignee=None, origin=None)


def _session(tmp_path: Path) -> Session:
    return Session(agent_name="alice", state_log=StateLog(tmp_path / "wal.jsonl"))


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_hook_self_continuation_create_is_owned_by_executing_task(tmp_path):
    """Tier 2: §16 B1.5 headline — while executing T, a HOOK self-continuation turn
    PRESERVES current=T, so a sub-task created on it is OWNED by T (through the REAL
    adapter op-ctx builder), NOT orphaned to the session. This is tui's iteration-cap
    scenario (the cap ends the turn → a hook self-continuation resumes). FALSIFY: if
    _stamp resets current_task_id on a hook turn → the sub-task falls to
    requester=session → RED."""
    s = _session(tmp_path)
    b = InMemoryTaskBackend()

    # #2186: the executing task's id must be a home-addressable task-ref so the derived
    # sub-task requester is self-identifying as task-owned (is_task_ref True).
    t_ref = make_task_ref("main")
    # the session is executing task-as-request T (set by the task_ready wake).
    s._stamp_execution_context(WAKE_READY_KIND, {"meta": {"task_id": t_ref}})
    # the capped turn ends → a hook self-continuation resumes the SAME execution.
    s._stamp_execution_context(HOOK_INBOX_KIND, {"text": "continue working on T"})

    # a sub-task created on the hook-continuation turn (real adapter builder) is T-owned.
    adapter = make_adapter(agent_name="alice", task_backend=b, session_id="main",
                           current_task_id_fn=lambda: s._current_task_id)
    ctx = adapter.make_router_op_context()
    res = await taskmod._create(_create_op("sub-on-hook"), ctx, "control_ir")
    sub = await b.get(res["task"]["task_id"])
    assert sub.requester == t_ref  # owned by T, not orphaned (RED if hook resets)
    assert is_task_ref(sub.requester)


@pytest.mark.asyncio
async def test_stamp_classifies_every_trigger_kind_complete_by_construction(tmp_path):
    """Tier 2: §16 B1.5 — EVERY trigger kind run_one_iteration dispatches is classified
    (no kind falls into the wrong band). Asserts on the built op-ctx (public output).
    SET on task wakes, PRESERVE on self-continuations (hook/agent_response/
    skill_completed), RESET on new contexts (user/agent_request) + an unknown
    (fail-safe to session-owned)."""
    s = _session(tmp_path)

    def current() -> "str | None":
        return s._make_router_op_context().current_task_id

    # SET: a task_ready wake introduces the execution context.
    s._stamp_execution_context(WAKE_READY_KIND, {"meta": {"task_id": "T"}})
    assert current() == "T"

    # PRESERVE: self-continuations keep T (the B1.5 fix).
    for k in (HOOK_INBOX_KIND, "agent_response", "skill_completed"):
        s._stamp_execution_context(k, {})
        assert current() == "T", f"{k} must PRESERVE the execution context"

    # RESET: a genuinely new external context clears to session-owned.
    for k in ("user", "agent_request", "some_unknown_future_kind"):
        s._stamp_execution_context(WAKE_READY_KIND, {"meta": {"task_id": "T"}})  # re-arm
        s._stamp_execution_context(k, {})
        assert current() is None, f"{k} must RESET to session-owned"
