"""Tier 2: #2186 — per-session Task isolation + the cross-ledger DAG orchestration.

A task lives in its assignee/executing session's OWN ledger (session-keyed, not
agent-keyed — supersedes #2128/#2180). `requester` is a bare home-addressable reference
(no requester_kind). Cross-session relations (dep edges, §16 ownership) resolve by
following the home-addressable ref across ledgers, via the OpContext resolver, with R1
durability barriers (dep target-durable-first / ownership marker-durable-first) and
abort/completion-time self-heal of the reverse-marker index.

Real AgentRegistry + real per-session SqliteTaskBackend + the real op handlers driven
through a real OpContext (no mocks — the harness exercises the actual resolver routing).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.task import (
    _abort,
    _add_dependency,
    _create,
    _update_status,
)
from reyn.runtime.registry import AgentRegistry
from reyn.task import SqliteTaskBackend, Task, TaskState
from reyn.task.ref import home_sid_of, is_task_ref, make_task_ref


class _RecordingWaker:
    """A real recording TaskWaker stand-in — records the assignee sids woken (no mocks)."""

    def __init__(self) -> None:
        self.ready: list[str] = []
        self.assigned: list[str] = []

    async def wake_ready_dependent(self, task, *, fenced_description=None) -> None:
        self.ready.append(task.assignee)

    async def wake_assigned(self, task, *, fenced_description=None) -> None:
        self.assigned.append(task.assignee)


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda _p: None, state_log=None)


def _ctx(reg: AgentRegistry, name: str, sid: str, *, waker=None) -> OpContext:
    return OpContext(
        workspace=None, events=None, permission_decl=None,
        task_backend=reg.task_backend_for(name, sid),
        task_backend_resolver=reg.task_backend_resolver_for(name),
        session_id=sid, task_waker=waker,
    )


def _create_op(name: str, *, assignee=None, deps=None):
    return SimpleNamespace(
        name=name, assignee=assignee, deps=deps or [], description=None, origin="self")


# ── the headline: per-session isolation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_task_lives_in_assignee_ledger_not_caller(tmp_path):
    """Tier 2: (THE HEADLINE) a task delegated by main to a worker session lives in the
    WORKER's ledger (home = assignee), NOT main's — per-session isolation. RED on the
    agent-shared model (it would land in the one shared ledger visible to both)."""
    reg = _registry(tmp_path)
    ctx = _ctx(reg, "a", "main")
    tid = (await _create(_create_op("deleg", assignee="worker:1"), ctx, "main"))["task"]["task_id"]
    assert home_sid_of(tid) == "worker:1"                     # home-addressed to the assignee
    assert await reg.task_backend_for("a", "worker:1").get(tid) is not None  # in worker's ledger
    assert await reg.task_backend_for("a", "main").get(tid) is None          # NOT in main's


@pytest.mark.asyncio
async def test_self_task_stays_in_callers_own_ledger(tmp_path):
    """Tier 2: a self-task (no/own assignee) stays in the caller's own ledger (the
    intra-ledger path — the resolver falls back to the caller's backend)."""
    reg = _registry(tmp_path)
    ctx = _ctx(reg, "a", "main")
    tid = (await _create(_create_op("selftask"), ctx, "main"))["task"]["task_id"]
    assert home_sid_of(tid) == "main"
    assert await reg.task_backend_for("a", "main").get(tid) is not None


def test_per_session_backend_keying_and_resolver(tmp_path):
    """Tier 2: task_backend_for is per-(name, sid) (distinct sessions → distinct ledgers);
    the resolver returns the SAME instance the foreign session uses (no N+1 connection)."""
    reg = _registry(tmp_path)
    m1, m2 = reg.task_backend_for("a", "main"), reg.task_backend_for("a", "main")
    w = reg.task_backend_for("a", "w:1")
    assert m1 is m2 and m1 is not w                           # per-(name,sid)
    assert reg.task_backend_resolver_for("a")("w:1") is w     # resolver → same instance


# ── cross-ledger dep: wake on completion + self-heal ────────────────────────


@pytest.mark.asyncio
async def test_cross_ledger_dep_blocks_then_completion_wakes_and_self_heals(tmp_path):
    """Tier 2: (falsification 4/5/6) a cross-ledger dependent BLOCKS; when the remote dep
    COMPLETES, the dependent's satisfied edge is removed → promoted → its assignee woken,
    and the consumed dep reverse-marker is DROPPED (strict zero-orphan). RED if completion
    didn't reach the cross-ledger dependent (it would stay BLOCKED forever)."""
    reg = _registry(tmp_path)
    waker = _RecordingWaker()
    main_ctx = _ctx(reg, "a", "main", waker=waker)
    worker_be = reg.task_backend_for("a", "worker:1")
    # worker owns Y; main owns X; X depends on Y (cross-ledger).
    y = (await _create(_create_op("Y", assignee="worker:1"), main_ctx, "main"))["task"]["task_id"]
    x = (await _create(_create_op("X", assignee="main"), main_ctx, "main"))["task"]["task_id"]
    await _add_dependency(SimpleNamespace(task_id=x, depends_on=y), main_ctx, "main")
    assert (await reg.task_backend_for("a", "main").get(x)).status is TaskState.BLOCKED
    assert await worker_be.remote_refs(y, "dependent") == [x]      # marker recorded
    # worker completes Y (in its own ledger / ctx)
    worker_ctx = _ctx(reg, "a", "worker:1", waker=waker)
    await worker_be.update_status(y, TaskState.IN_PROGRESS, caller_session_id="worker:1")
    await _update_status(SimpleNamespace(task_id=y, status="completed"), worker_ctx, "worker:1")
    assert (await reg.task_backend_for("a", "main").get(x)).status is TaskState.READY  # promoted
    assert "main" in waker.ready                                   # X's assignee woken
    assert await worker_be.remote_refs(y, "dependent") == []       # self-healed (zero-orphan)


# ── cross-ledger ownership cascade: abort + self-heal ───────────────────────


@pytest.mark.asyncio
async def test_cross_ledger_owned_child_aborted_on_parent_abort(tmp_path):
    """Tier 2: (falsification 8) a sub-task DELEGATED to a worker but OWNED by a parent task
    in main's ledger IS aborted when the parent is aborted — via the cross-ledger ownership
    reverse-marker. RED on a local-only abort BFS (the worker-ledger child would be orphaned,
    left non-terminal after its owner aborts)."""
    reg = _registry(tmp_path)
    main_ctx = _ctx(reg, "a", "main")
    worker_be = reg.task_backend_for("a", "worker:1")
    parent = (await _create(_create_op("parent", assignee="main"), main_ctx, "main"))["task"]["task_id"]
    # main executes `parent` → a sub-task delegated to worker is OWNED by parent (cross-ledger).
    exec_ctx = _ctx(reg, "a", "main")
    exec_ctx.current_task_id = parent
    child = (await _create(_create_op("child", assignee="worker:1"), exec_ctx, "main"))["task"]["task_id"]
    assert is_task_ref((await worker_be.get(child)).requester)        # owned by parent (a task-ref)
    assert await reg.task_backend_for("a", "main").remote_refs(parent, "child") == [child]
    # abort the parent → the cross-ledger child must be archived too
    await _abort(SimpleNamespace(task_id=parent, reason="x"), main_ctx, "main")
    assert (await worker_be.get(child)).status is TaskState.ARCHIVED  # cross-ledger cascade
    assert await reg.task_backend_for("a", "main").remote_refs(parent, "child") == []  # self-healed


# ── §13 cross-ledger cycle detection ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_ledger_cycle_detected_and_rejected(tmp_path):
    """Tier 2: (falsification 10, §13) X@main depends on Y@worker; then Y@worker depends on
    X@main would close a CROSS-LEDGER cycle → the second add_dependency detects it via the
    resolver-BFS + REJECTS (decision-enabling cycle error). RED on a local-only cycle-check
    (neither ledger sees the full loop → both tasks would deadlock BLOCKED forever)."""
    reg = _registry(tmp_path)
    # main is the REQUESTER of both X and Y (it created them — add_dependency is
    # requester-gated), so both edge ops are driven from main's ctx; X lives in main's
    # ledger, Y in worker's (cross-ledger).
    main_ctx = _ctx(reg, "a", "main")
    x = (await _create(_create_op("X", assignee="main"), main_ctx, "main"))["task"]["task_id"]
    y = (await _create(_create_op("Y", assignee="worker:1"), main_ctx, "main"))["task"]["task_id"]
    ok = await _add_dependency(SimpleNamespace(task_id=x, depends_on=y), main_ctx, "main")
    assert ok["status"] == "ok"                                    # X→Y fine
    # Y→X would close the cross-ledger cycle → rejected via the resolver-BFS.
    cyc = await _add_dependency(SimpleNamespace(task_id=y, depends_on=x), main_ctx, "main")
    assert cyc["status"] == "error" and cyc["error"]["kind"] == "cycle"


# ── clean-break ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clean_break_discards_pre_2186_db(tmp_path):
    """Tier 2: a pre-#2186 (old-format: requester_kind column, bare-uuid rows) db is
    DISCARDED on first open — no compat read, no half-old-half-new ledger (owner
    clean-break). Observable via the public surface: the old bare-uuid row is gone, and a
    fresh home-addressable task round-trips in the new schema. RED if old rows survived."""
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, requester_kind TEXT, name TEXT)")
    c.execute("INSERT INTO tasks VALUES('bare-uuid','session','old')")
    c.commit()
    c.close()
    b = SqliteTaskBackend(str(p))
    assert await b.get("bare-uuid") is None                        # old-format row discarded
    tid = make_task_ref("main")
    await b.create(Task(task_id=tid, name="fresh", assignee="main", requester="main"))
    assert (await b.get(tid)) is not None                          # new schema works
    b.close()
