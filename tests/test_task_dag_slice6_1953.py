"""Tier 2b: #1953 slice 6 — Task dependency-DAG (cycle-check + completion-driven readiness).

The dependency DAG (§13) stays acyclic-by-construction (cycle-forming edges
rejected at add-time via a shared helper on BOTH create(deps) and add_dependency)
and a predecessor reaching ``completed`` drives OS-authority readiness recompute
(a fully-satisfied dependent flips ``blocked → ready`` without the assignee CAS).
Real sqlite backend + in-memory backend (parametrized); no mocks.

Falsification (per axis, CLEAN-RED):
- cycle rejected (self / 2- / N-cycle) with the offending path; non-cycle DAG accepted;
- a dangling dep (OQ-1) rejected on both create and add_dependency;
- create with incomplete deps is born-blocked; deps-less create keeps its status;
- a completed predecessor promotes a fully-satisfied dependent + emits P6; a
  partially-satisfied dependent stays blocked;
- the readiness write is OS-authority (no assignee session); it persists across a
  sqlite reload;
- the op layer maps a rejected edge to a decision-enabling error dict (OQ-5), and
  completion through ``task.update_status`` drives the recompute + the P6 event.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import (
    InMemoryTaskBackend,
    SqliteTaskBackend,
    Task,
    TaskCycleError,
    TaskDepNotFoundError,
    TaskState,
)
from reyn.task.backend import find_cycle_path


def _task(task_id, *, deps=None, status=TaskState.PENDING, assignee="sess", requester="req"):
    return Task(task_id=task_id, name=task_id, assignee=assignee, requester=requester,
                status=status, deps=list(deps or []))


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    if request.param == "inmem":
        yield InMemoryTaskBackend()
    else:
        b = SqliteTaskBackend(tmp_path / "dag.db")
        yield b
        b.close()


# ── cycle-check (OQ-4/OQ-5) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_cycle_rejected(backend):
    """Tier 2b: a self-loop edge (a→a) is a cycle."""
    await backend.create(_task("a"))
    with pytest.raises(TaskCycleError):
        await backend.add_dependency("a", "a")


@pytest.mark.asyncio
async def test_two_cycle_rejected(backend):
    """Tier 2b: a 2-cycle (a→b then b→a) is rejected at the closing edge."""
    await backend.create(_task("a"))
    await backend.create(_task("b"))
    await backend.add_dependency("a", "b")
    with pytest.raises(TaskCycleError):
        await backend.add_dependency("b", "a")


@pytest.mark.asyncio
async def test_n_cycle_rejected_with_path(backend):
    """Tier 2b: an N-cycle (a→b→c then c→a) is rejected; the error carries the
    offending cycle path (decision-enabling, OQ-5)."""
    for t in ("a", "b", "c"):
        await backend.create(_task(t))
    await backend.add_dependency("a", "b")
    await backend.add_dependency("b", "c")
    with pytest.raises(TaskCycleError) as ei:
        await backend.add_dependency("c", "a")
    # path closes the cycle: c -> ... -> c.
    assert ei.value.path[0] == "c" and ei.value.path[-1] == "c"


@pytest.mark.asyncio
async def test_non_cycle_dag_accepted(backend):
    """Tier 2b: a diamond DAG (no cycle) is accepted on every edge."""
    for t in ("a", "b", "c"):
        await backend.create(_task(t))
    await backend.add_dependency("a", "b")
    await backend.add_dependency("a", "c")
    await backend.add_dependency("b", "c")
    got = await backend.get("a")
    assert set(got.deps) == {"b", "c"}


# ── existence (OQ-1) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dangling_dep_rejected_on_add(backend):
    """Tier 2b: add_dependency to a non-existent task is rejected (OQ-1)."""
    await backend.create(_task("a"))
    with pytest.raises(TaskDepNotFoundError):
        await backend.add_dependency("a", "ghost")


@pytest.mark.asyncio
async def test_dangling_dep_rejected_on_create(backend):
    """Tier 2b: create(deps=[non-existent]) is rejected — the shared helper guards
    the create path too (OQ-4 completeness)."""
    with pytest.raises(TaskDepNotFoundError):
        await backend.create(_task("a", deps=["ghost"]))


# ── born-blocked (OQ-3 origin of `blocked`) ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_with_incomplete_deps_is_born_blocked(backend):
    """Tier 2b: a task born with not-all-completed deps is OS-derived blocked."""
    await backend.create(_task("d"))  # pending, not completed
    await backend.create(_task("a", deps=["d"], status=TaskState.PENDING))
    assert (await backend.get("a")).status is TaskState.BLOCKED


@pytest.mark.asyncio
async def test_create_without_deps_keeps_requested_status(backend):
    """Tier 2b: a deps-less create keeps its requested status (the A2A path is
    unaffected — OQ-2: birth-derivation only fires when deps are present)."""
    await backend.create(_task("a", status=TaskState.IN_PROGRESS))
    assert (await backend.get("a")).status is TaskState.IN_PROGRESS


@pytest.mark.asyncio
async def test_create_with_already_completed_deps_not_blocked(backend):
    """Tier 2b: if every born-with dep is already completed, the task is not
    born-blocked (nothing to wait for)."""
    await backend.create(_task("d", assignee="sd"))
    await backend.update_status("d", "completed", caller_session_id="sd")
    await backend.create(_task("a", deps=["d"], status=TaskState.PENDING))
    assert (await backend.get("a")).status is TaskState.PENDING


# ── completion-driven readiness (OQ-3) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_predecessor_promotes_satisfied_dependent(backend):
    """Tier 2b: a predecessor reaching completed → a fully-satisfied dependent
    flips blocked → ready."""
    await backend.create(_task("d", assignee="sd"))
    await backend.create(_task("a", deps=["d"]))
    assert (await backend.get("a")).status is TaskState.BLOCKED
    await backend.update_status("d", "completed", caller_session_id="sd")
    promoted = await backend.recompute_readiness("d")
    assert [t.task_id for t in promoted] == ["a"]
    assert (await backend.get("a")).status is TaskState.READY


@pytest.mark.asyncio
async def test_partially_satisfied_dependent_stays_blocked(backend):
    """Tier 2b: a dependent with one of two deps completed stays blocked."""
    await backend.create(_task("d1", assignee="s1"))
    await backend.create(_task("d2", assignee="s2"))
    await backend.create(_task("a", deps=["d1", "d2"]))
    await backend.update_status("d1", "completed", caller_session_id="s1")
    promoted = await backend.recompute_readiness("d1")
    assert promoted == []
    assert (await backend.get("a")).status is TaskState.BLOCKED


@pytest.mark.asyncio
async def test_recompute_is_os_authority_no_assignee_session(backend):
    """Tier 2b: the blocked→ready write takes NO caller_session_id and bypasses
    the assignee CAS (OS scheduling, P3) — the dependent's assignee is a different
    session, yet the OS promotes it with no session context."""
    await backend.create(_task("d", assignee="sd"))
    await backend.create(_task("a", deps=["d"], assignee="sa"))
    await backend.update_status("d", "completed", caller_session_id="sd")
    promoted = await backend.recompute_readiness("d")  # no caller identity at all
    assert [t.task_id for t in promoted] == ["a"]


@pytest.mark.asyncio
async def test_readiness_and_edges_persist_across_sqlite_reload(tmp_path):
    """Tier 2b: the promoted readiness + the dependency edge survive a sqlite
    close + reopen from disk (durable, not in-memory cache)."""
    path = tmp_path / "persist.db"
    b = SqliteTaskBackend(path)
    await b.create(_task("d", assignee="sd"))
    await b.create(_task("a", deps=["d"]))
    await b.update_status("d", "completed", caller_session_id="sd")
    await b.recompute_readiness("d")
    b.close()

    reopened = SqliteTaskBackend(path)
    a = await reopened.get("a")
    assert a is not None and a.status is TaskState.READY and a.deps == ["d"]
    reopened.close()


# ── pure cycle helper ───────────────────────────────────────────────────────


def test_find_cycle_path_detects_and_clears():
    """Tier 2b: the shared pure cycle helper returns the closing path for a
    cycle-forming edge and None for a non-cycle (diamond) edge."""
    graph = {"a": ["b"], "b": ["c"], "c": []}
    deps_of = lambda n: graph.get(n, [])  # noqa: E731
    # c→a closes a→b→c→a.
    assert find_cycle_path(deps_of, "c", "a") == ["c", "a", "b", "c"]
    # a→c is a diamond shortcut, no cycle (c has no path back to a).
    assert find_cycle_path(deps_of, "a", "c") is None


# ── op layer: OQ-5 error dict + completion-driven recompute + P6 ────────────


class _Recorder:
    """A real recording event log (not a mock) capturing emit() calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields) -> None:
        self.events.append((kind, fields))


def _opctx(backend, *, events=None, session_id="req"):
    return SimpleNamespace(session_id=session_id, agent_id="a",
                           events=events, task_backend=backend)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_op_add_dependency_cycle_returns_error_dict():
    """Tier 2b: a cycle-forming edge through the op layer returns a structured
    error dict (OQ-5) — not a raised exception through the dispatcher."""
    b = InMemoryTaskBackend()
    await b.create(_task("a", requester="req"))
    await b.create(_task("b", requester="req"))
    await taskmod._add_dependency(SimpleNamespace(task_id="a", depends_on="b"),
                                  _opctx(b), "control_ir")
    res = await taskmod._add_dependency(SimpleNamespace(task_id="b", depends_on="a"),
                                        _opctx(b), "control_ir")
    assert res["status"] == "error"
    assert res["error"]["kind"] == "cycle"
    assert res["error"]["edge"] == ["b", "a"]
    assert res["error"]["path"][0] == "b" and res["error"]["path"][-1] == "b"


@pytest.mark.asyncio
async def test_op_create_dangling_dep_returns_error_dict():
    """Tier 2b: create(deps=[non-existent]) through the op layer returns the
    dep_not_found error dict (OQ-1/OQ-5)."""
    b = InMemoryTaskBackend()
    op = SimpleNamespace(name="x", assignee=None, origin="self", description=None,
                         budget_cap=None, deps=["ghost"], parent_id=None)
    res = await taskmod._create(op, _opctx(b), "control_ir")
    assert res["status"] == "error"
    assert res["error"]["kind"] == "dep_not_found"
    assert res["error"]["edge"][1] == "ghost"


@pytest.mark.asyncio
async def test_op_update_status_completion_drives_readiness_and_emits_p6():
    """Tier 2b: completing a predecessor through task.update_status drives the
    OS readiness recompute and emits a generic P6 task_readiness event."""
    b = InMemoryTaskBackend()
    rec = _Recorder()
    await b.create(_task("d", assignee="sd"))
    await b.create(_task("a", deps=["d"], assignee="sa"))
    # Complete d (ctx.session_id must equal d's assignee for the CAS).
    res = await taskmod._update_status(
        SimpleNamespace(task_id="d", status="completed"),
        _opctx(b, events=rec, session_id="sd"), "control_ir")
    assert res["status"] == "ok"
    # a was promoted → exactly one task_readiness event for a (behavioral, not a
    # size pin): which task readied, triggered by which predecessor.
    readied = [(f["task_id"], f["trigger"]) for k, f in rec.events if k == "task_readiness"]
    assert readied == [("a", "d")]
    assert (await b.get("a")).status is TaskState.READY


@pytest.mark.asyncio
async def test_op_update_status_non_completion_does_not_recompute():
    """Tier 2b: a non-completed transition (e.g. in_progress) does NOT drive
    readiness — only `completed` satisfies a dependency edge."""
    b = InMemoryTaskBackend()
    rec = _Recorder()
    await b.create(_task("d", assignee="sd"))
    await b.create(_task("a", deps=["d"], assignee="sa"))
    await taskmod._update_status(
        SimpleNamespace(task_id="d", status="in_progress"),
        _opctx(b, events=rec, session_id="sd"), "control_ir")
    assert [k for k, _ in rec.events if k == "task_readiness"] == []
    assert (await b.get("a")).status is TaskState.BLOCKED
