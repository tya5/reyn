"""Tier 1/2: #1953 slice 6-ext — mutable dependency ops + abort/failed→REQUESTER routing.

Extends slice 6 with `task.remove_dependency` / `task.repoint_dependency` (the
requester's recovery moves) over the shared OS-authority readiness primitive
(`_derive_readiness`: promote/demote across the pre-run states only), and routes a
non-completed terminal (aborted/failed) with still-alive dependents to the task's
REQUESTER (§16 / #2107; the prior slice-6-ext parent-keyed routing dropped root tasks).
The wake is the slice-7 TaskWaker, stubbed here via a recording waker. Real sqlite +
in-memory backends; no mocks.

Falsification per axis (CLEAN-RED): remove promotes a now-satisfied dependent +
the I-1 last-dep case + idempotent + never-demotes; repoint cycle/dangling rejected
ATOMICALLY (graph unchanged) + demote + promote + in_progress-untouched; the
abort/failed routing fires the waker + P6 to the right REQUESTER/dependents, a ROOT
self-task is NOT dropped (#2107), and an EXTERNAL-origin task skips the internal wake.
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


def _task(task_id, *, deps=None, status=TaskState.READY, assignee="sess",
          requester="req", origin=None):
    kw = {} if origin is None else {"origin": origin}
    return Task(task_id=task_id, name=task_id, assignee=assignee, requester=requester,
                status=status, deps=list(deps or []), **kw)


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    if request.param == "inmem":
        yield InMemoryTaskBackend()
    else:
        b = SqliteTaskBackend(tmp_path / "ext.db")
        yield b
        b.close()


async def _complete(backend, task_id, assignee):
    await backend.update_status(task_id, "done", caller_session_id=assignee)


# ── remove_dependency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_promotes_now_satisfied_dependent(backend):
    """Tier 2: dropping the last unsatisfied edge promotes a blocked dependent."""
    await backend.create(_task("d1", assignee="s1"))
    await backend.create(_task("d2", assignee="s2"))
    await backend.create(_task("a", deps=["d1", "d2"]))     # born-blocked
    await _complete(backend, "d1", "s1")
    await backend.recompute_readiness("d1")                 # still blocked (d2 pending)
    assert (await backend.get("a")).status is TaskState.BLOCKED

    await backend.remove_dependency("a", "d2")              # only d1 (completed) left
    assert (await backend.get("a")).status is TaskState.READY


@pytest.mark.asyncio
async def test_remove_last_dep_readies_i1(backend):
    """Tier 2: I-1 — removing the LAST dep readies an ordering-free task."""
    await backend.create(_task("d1", assignee="s1"))
    await backend.create(_task("a", deps=["d1"]))           # born-blocked
    assert (await backend.get("a")).status is TaskState.BLOCKED

    await backend.remove_dependency("a", "d1")
    a = await backend.get("a")
    assert a.status is TaskState.READY and a.deps == []


@pytest.mark.asyncio
async def test_remove_is_idempotent_on_missing_edge(backend):
    """Tier 2: removing an absent edge is a no-op (no raise, no status flip)."""
    await backend.create(_task("a", status=TaskState.RUNNING))
    task = await backend.remove_dependency("a", "never-there")
    assert task is not None and task.status is TaskState.RUNNING


@pytest.mark.asyncio
async def test_remove_never_demotes(backend):
    """Tier 2: dropping an edge only relaxes — a ready task stays ready."""
    await backend.create(_task("d1", assignee="s1"))
    await _complete(backend, "d1", "s1")
    await backend.create(_task("a", deps=["d1"]))           # deps all completed → not born-blocked
    await backend.create(_task("d2", assignee="s2"))
    await backend.add_dependency("a", "d2")                 # a now has d2 (incomplete) — pure topology
    # a is PENDING (add_dependency doesn't re-block, OQ-2). Removing d2 keeps it PENDING.
    before = (await backend.get("a")).status
    task = await backend.remove_dependency("a", "d1")
    assert task.status is before  # relax-only never demotes a non-blocked task


# ── repoint_dependency ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repoint_cycle_rejected_atomically(backend):
    """Tier 2: a cycle-forming repoint raises AND leaves the graph unchanged."""
    await backend.create(_task("x", assignee="sx"))
    await backend.create(_task("a", deps=["x"]))
    await backend.create(_task("b", deps=["a"]))            # b → a
    with pytest.raises(TaskCycleError):
        await backend.repoint_dependency("a", "x", "b")     # a → b would close a→b→a
    # ATOMIC: a still depends on x only (nothing changed).
    assert (await backend.get("a")).deps == ["x"]


@pytest.mark.asyncio
async def test_repoint_dangling_rejected_atomically(backend):
    """Tier 2: a repoint to a non-existent task raises + changes nothing."""
    await backend.create(_task("x", assignee="sx"))
    await backend.create(_task("a", deps=["x"]))
    with pytest.raises(TaskDepNotFoundError):
        await backend.repoint_dependency("a", "x", "ghost")
    assert (await backend.get("a")).deps == ["x"]


@pytest.mark.asyncio
async def test_repoint_promotes_when_new_edge_satisfied(backend):
    """Tier 2: repointing a blocked task onto a completed substitute readies it."""
    await backend.create(_task("x", assignee="sx"))         # incomplete
    await backend.create(_task("y", assignee="sy"))
    await _complete(backend, "y", "sy")                     # completed substitute
    await backend.create(_task("a", deps=["x"]))            # born-blocked on x
    assert (await backend.get("a")).status is TaskState.BLOCKED

    await backend.repoint_dependency("a", "x", "y")
    a = await backend.get("a")
    assert a.status is TaskState.READY and a.deps == ["y"]


@pytest.mark.asyncio
async def test_repoint_demotes_when_new_edge_unsatisfied(backend):
    """Tier 2: repointing a pre-run task onto an incomplete substitute re-blocks it
    (repoint is the full re-derive — allow_demote)."""
    await backend.create(_task("x", assignee="sx"))
    await _complete(backend, "x", "sx")
    await backend.create(_task("y", assignee="sy"))         # incomplete
    await backend.create(_task("a", deps=["x"]))            # x completed → PENDING (pre-run, demotable)
    assert (await backend.get("a")).status is TaskState.READY

    await backend.repoint_dependency("a", "x", "y")         # now depends on incomplete y
    assert (await backend.get("a")).status is TaskState.BLOCKED


@pytest.mark.asyncio
async def test_derive_readiness_leaves_in_progress_untouched(backend):
    """Tier 2: load-bearing single-writer — repointing a dep of an IN_PROGRESS task
    does NOT re-block it (the OS schedules pre-run states; the assignee owns the run)."""
    await backend.create(_task("x", assignee="sx"))
    await _complete(backend, "x", "sx")
    await backend.create(_task("y", assignee="sy"))         # incomplete
    await backend.create(_task("a", deps=["x"], assignee="sa"))
    await backend.update_status("a", "running", caller_session_id="sa")
    assert (await backend.get("a")).status is TaskState.RUNNING

    await backend.repoint_dependency("a", "x", "y")         # new incomplete dep
    # untouched: the assignee owns the run, OS does not yank it back to blocked.
    a = await backend.get("a")
    assert a.status is TaskState.RUNNING and a.deps == ["y"]


@pytest.mark.asyncio
async def test_dependents_reverse_lookup(backend):
    """Tier 2: dependents(x) returns every task that depends ON x."""
    await backend.create(_task("x", assignee="sx"))
    await backend.create(_task("a", deps=["x"]))
    await backend.create(_task("b", deps=["x"]))
    await backend.create(_task("c", assignee="sc"))         # unrelated
    deps = await backend.dependents("x")
    assert sorted(d.task_id for d in deps) == ["a", "b"]


@pytest.mark.asyncio
async def test_repoint_persists_across_sqlite_reload(tmp_path):
    """Tier 2: the repointed edge + the re-derived readiness survive a reload."""
    path = tmp_path / "persist.db"
    b = SqliteTaskBackend(path)
    await b.create(_task("x", assignee="sx"))
    await b.create(Task(task_id="y", name="y", assignee="sy", requester="r",
                        status=TaskState.DONE))
    await b.create(_task("a", deps=["x"]))                  # born-blocked
    await b.repoint_dependency("a", "x", "y")               # → ready (y completed)
    b.close()
    reopened = SqliteTaskBackend(path)
    a = await reopened.get("a")
    assert a is not None and a.status is TaskState.READY and a.deps == ["y"]
    reopened.close()


# ── op-layer: edge-error dict + P6 readiness + abort/failed→parent routing ──


class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **f) -> None:
        self.events.append((kind, f))


class _RecordingWaker:
    """A real (non-mock) injectable TaskWaker recording its requester-notify calls (§16)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify_requester_decide(self, *, requester_session, terminal_task, dependents,
                                      disposition=None, managing_task_id=None):
        self.calls.append({
            "requester_session": requester_session,
            "terminal_task": terminal_task.task_id,
            "dependents": [d.task_id for d in dependents],
            "disposition": disposition,
            "managing_task_id": managing_task_id,
        })

    async def publish_task_event(self, event_type, task, **kwargs):
        """#2187 Stage 4: mirror TaskWaker.publish_task_event's dispatch — the op now
        publishes through the single seam; route the terminal event to the recorded
        requester-notify (this stub records only the §16 requester-decide path)."""
        if event_type == "terminal":
            await self.notify_requester_decide(terminal_task=task, **kwargs)


def _opctx(backend, *, events=None, waker=None, session_id="req"):
    return SimpleNamespace(session_id=session_id, agent_id="a",
                           events=events, task_backend=backend, task_waker=waker)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_op_repoint_cycle_returns_edge_error_dict():
    """Tier 2: a cycle-forming repoint through the op layer returns the structured
    error dict (OQ-5 shape) — not a raised exception."""
    b = InMemoryTaskBackend()
    await b.create(_task("x", assignee="sx", requester="req"))
    await b.create(_task("a", deps=["x"], requester="req"))
    await b.create(_task("bb", deps=["a"], requester="req"))
    res = await taskmod._repoint_dependency(
        SimpleNamespace(task_id="a", from_depends_on="x", to_depends_on="bb"),
        _opctx(b))
    assert res["status"] == "error"
    assert res["error"]["kind"] == "cycle"
    assert (await b.get("a")).deps == ["x"]  # unchanged


@pytest.mark.asyncio
async def test_op_remove_emits_readiness_on_promote():
    """Tier 2: a remove that promotes a dependent emits the generic P6
    task_readiness (to=ready)."""
    b = InMemoryTaskBackend()
    rec = _Rec()
    await b.create(_task("d1", assignee="s1", requester="req"))
    await b.create(_task("a", deps=["d1"], requester="req"))  # born-blocked
    res = await taskmod._remove_dependency(
        SimpleNamespace(task_id="a", depends_on="d1"),
        _opctx(b, events=rec, session_id="req"))
    assert res["status"] == "ok"
    readied = [(f["task_id"], f["to"]) for k, f in rec.events if k == "task_readiness"]
    assert readied == [("a", "ready")]


@pytest.mark.asyncio
async def test_op_abort_routes_disposition_to_requester():
    """Tier 2: §16 — aborting a task with a still-alive sibling dependent routes the
    disposition to the task's REQUESTER (not the parent) — recording waker fired + P6."""
    b = InMemoryTaskBackend()
    rec = _Rec()
    waker = _RecordingWaker()
    await b.create(_task("B", assignee="sB", requester="req", status=TaskState.RUNNING))
    await b.create(_task("A", deps=["B"], assignee="sA", requester="req"))

    await taskmod._abort(SimpleNamespace(task_id="B", reason=None),
                         _opctx(b, events=rec, waker=waker, session_id="req"))

    assert waker.calls == [{"requester_session": "req", "terminal_task": "B",
                            "dependents": ["A"], "disposition": "aborted",
                            "managing_task_id": None}]
    routed = [f for k, f in rec.events if k == "task_dependency_aborted"]
    assert routed and routed[0]["requester"] == "req" and routed[0]["dependents"] == ["A"]
    assert routed[0]["disposition"] == "aborted"


@pytest.mark.asyncio
async def test_op_failed_routes_disposition_to_requester():
    """Tier 2: §16 — a `failed` declaration (assignee) with dependents routes to the
    task's REQUESTER."""
    b = InMemoryTaskBackend()
    waker = _RecordingWaker()
    await b.create(_task("B", assignee="sB", requester="req", status=TaskState.RUNNING))
    await b.create(_task("A", deps=["B"], assignee="sA", requester="req"))

    # failed is assignee-gated → caller must be B's assignee.
    await taskmod._update_status(SimpleNamespace(task_id="B", status="failed"),
                                 _opctx(b, waker=waker, session_id="sB"))
    assert waker.calls == [{"requester_session": "req", "terminal_task": "B",
                            "dependents": ["A"], "disposition": "failed",
                            "managing_task_id": None}]


@pytest.mark.asyncio
async def test_op_abort_root_routes_to_requester():
    """Tier 2: §16/#2107 — a ROOT task (no parent) with a stuck dependent DOES route
    its disposition — to the REQUESTER. The prior parent-keyed routing dropped it (no
    parent → `if not parent_id: return`), silently stranding a flat self-task plan's
    dependents. Now the requester (always present) is woken to recover."""
    b = InMemoryTaskBackend()
    waker = _RecordingWaker()
    await b.create(_task("B", assignee="sB", requester="req", status=TaskState.RUNNING))
    await b.create(_task("A", deps=["B"], assignee="sA", requester="req"))  # root, no parent
    await taskmod._abort(SimpleNamespace(task_id="B", reason=None),
                         _opctx(b, waker=waker, session_id="req"))
    assert waker.calls == [{"requester_session": "req", "terminal_task": "B",
                            "dependents": ["A"], "disposition": "aborted",
                            "managing_task_id": None}]  # #2107: NOT dropped


@pytest.mark.asyncio
async def test_2107_flat_self_task_plan_mid_failure_notifies_requester():
    """Tier 2: §16/#2107 repro — a FLAT self-task plan (t1→t2→t3→t4, all root, no
    parent) whose middle task is aborted notifies the REQUESTER to recover the stuck
    dependents — the wake fires; they are NOT silently stranded. The exact #2107 shape
    (prior parent-keyed routing dropped it on the missing parent_id). Revert the re-key
    (restore `if not parent_id: return`) → no requester notify → RED."""
    b = InMemoryTaskBackend()
    waker = _RecordingWaker()
    await b.create(_task("t1", assignee="me", requester="me", status=TaskState.DONE))
    await b.create(_task("t2", deps=["t1"], assignee="me", requester="me",
                         status=TaskState.RUNNING))
    await b.create(_task("t3", deps=["t2"], assignee="me", requester="me"))
    await b.create(_task("t4", deps=["t3"], assignee="me", requester="me"))
    await taskmod._abort(SimpleNamespace(task_id="t2", reason=None),
                         _opctx(b, waker=waker, session_id="me"))
    # the requester ("me") is woken to recover the stuck dependent t3 (t4 sits blocked
    # behind t3, surfaced once t3 is recovered) — NOT dropped.
    assert waker.calls == [{"requester_session": "me", "terminal_task": "t2",
                            "dependents": ["t3"], "disposition": "aborted",
                            "managing_task_id": None}]


@pytest.mark.asyncio
async def test_external_origin_skips_internal_requester_wake():
    """Tier 2: §16 S1 boundary — an EXTERNAL-origin terminal task does NOT fire the
    internal requester-session wake — its disposition rides the separate webhook channel
    (the abort-all + propagate is §16 S2). S1 preserves the prior external behavior."""
    from reyn.task.model import TaskOrigin
    b = InMemoryTaskBackend()
    waker = _RecordingWaker()
    await b.create(_task("B", assignee="sB", requester="a2a:client",
                         origin=TaskOrigin.EXTERNAL, status=TaskState.RUNNING))
    await b.create(_task("A", deps=["B"], assignee="sA", requester="a2a:client",
                         origin=TaskOrigin.EXTERNAL))
    await taskmod._abort(SimpleNamespace(task_id="B", reason=None),
                         _opctx(b, waker=waker, session_id="req"))
    assert waker.calls == []  # external → webhook channel (S2), not an internal session wake
