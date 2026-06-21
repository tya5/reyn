"""Tier 2: #1953 slice 8 — per-Task cap-hit as a first-class disposition.

`record_cost(task_id, delta)` is the per-Task cost-attribution primitive; the
OS-internal `record_task_cost` helper enforces a per-Task `budget_cap`: on a
cap-hit it force-terminates the task (abort-like — out of budget) and routes a
**first-class `cap_exceeded`** disposition to the parent via the SAME parent-LLM
seam as abort/failed (the "one decision resolves OQ-7 + cap-hit" property). The
no-conflation invariant: a budget cap-hit must be distinguishable from a genuine
error `failed` — `cap_exceeded` rides BOTH the P6 `task_disposition` event AND the
parent wake payload. Per-Task is an INDEPENDENT cap dimension (§I-3 (A)).

Real sqlite + in-memory backends + a real recording registry (TaskWaker); no mocks.

Note: `record_task_cost` is the **tested primitive** — the production cost-path
attribution (wiring the LLM cost recorder to it with a task_id) is a deliberate,
tracked defer that co-lands with the task-execution engine (slice P).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.runtime.services.task_wake import TaskWaker
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState


def _task(task_id, *, deps=None, status=TaskState.PENDING, assignee="sess",
          requester="req", parent_id=None, budget_cap=None):
    return Task(task_id=task_id, name=task_id, assignee=assignee, requester=requester,
                status=status, deps=list(deps or []), parent_id=parent_id,
                budget_cap=budget_cap)


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    if request.param == "inmem":
        yield InMemoryTaskBackend()
    else:
        b = SqliteTaskBackend(tmp_path / "cost.db")
        yield b
        b.close()


# ── record_cost primitive ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_cost_accumulates(backend):
    """Tier 2: record_cost adds onto cost_accum (the per-Task counter)."""
    await backend.create(_task("t"))
    await backend.record_cost("t", 1.5)
    await backend.record_cost("t", 2.0)
    assert (await backend.get("t")).cost_accum == 3.5


@pytest.mark.asyncio
async def test_record_cost_unknown_task_is_none(backend):
    """Tier 2: record_cost on an unknown task is a None no-op (no row created)."""
    assert await backend.record_cost("nope", 1.0) is None


@pytest.mark.asyncio
async def test_record_cost_persists_across_sqlite_reload(tmp_path):
    """Tier 2: cost_accum survives a sqlite close + reopen (durable counter)."""
    path = tmp_path / "persist.db"
    b = SqliteTaskBackend(path)
    await b.create(_task("t"))
    await b.record_cost("t", 4.25)
    b.close()
    re = SqliteTaskBackend(path)
    assert (await re.get("t")).cost_accum == 4.25
    re.close()


# ── cap-hit → cap_exceeded disposition → parent routing ─────────────────────


class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **f) -> None:
        self.events.append((kind, f))


class _RecSession:
    def __init__(self) -> None:
        self.inbox: list[tuple[str, dict]] = []

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        self.inbox.append((kind, payload))
        return "m"


class _RecRegistry:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, str]] = []
        self._sessions: dict[tuple[str, str, str], _RecSession] = {}

    def resolve_session(self, agent, transport, native_id):
        return self._sessions.setdefault((agent, transport, native_id), _RecSession())

    def ensure_session_running(self, name, sid):
        self.ensured.append((name, sid))

    def inbox_of(self, agent, transport, native_id):
        return self._sessions[(agent, transport, native_id)].inbox


def _ctx(backend, waker, *, events=None, session_id="req"):
    return SimpleNamespace(session_id=session_id, agent_id="alice", events=events,
                           task_backend=backend, task_waker=waker)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_under_cap_does_not_terminate():
    """Tier 2: cost under the cap leaves the task alive (nothing routed)."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    await b.create(_task("t", budget_cap=10.0, assignee="a2a:st",
                         status=TaskState.IN_PROGRESS))
    res = await taskmod.record_task_cost(_ctx(b, TaskWaker(reg, "alice")), "t", 3.0)
    assert res.status is TaskState.IN_PROGRESS
    assert reg.ensured == []  # no wake


@pytest.mark.asyncio
async def test_uncapped_task_never_terminates():
    """Tier 2: a task with no budget_cap accrues cost without termination."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    await b.create(_task("t", assignee="a2a:st", status=TaskState.IN_PROGRESS))
    res = await taskmod.record_task_cost(_ctx(b, TaskWaker(reg, "alice")), "t", 999.0)
    assert res.status is TaskState.IN_PROGRESS and res.cost_accum == 999.0


@pytest.mark.asyncio
async def test_cap_hit_terminates_and_routes_cap_exceeded_first_class():
    """Tier 2: a cap-hit force-terminates the task and routes a FIRST-CLASS
    `cap_exceeded` disposition to the parent — in BOTH the P6 event and the parent
    payload (the no-conflation invariant: NOT `failed`, NOT `aborted`)."""
    b = InMemoryTaskBackend()
    rec = _Rec()
    reg = _RecRegistry()
    await b.create(_task("P", assignee="a2a:sP", status=TaskState.IN_PROGRESS))
    await b.create(_task("B", budget_cap=5.0, assignee="a2a:sB", parent_id="P",
                         status=TaskState.IN_PROGRESS))
    await b.create(_task("A", deps=["B"], assignee="a2a:sA", parent_id="P"))  # stuck dependent

    await taskmod.record_task_cost(_ctx(b, TaskWaker(reg, "alice"), events=rec), "B", 6.0)

    # B force-terminated (archived).
    assert (await b.get("B")).status is TaskState.ARCHIVED
    # P6 task_disposition carries cap_exceeded first-class (not aborted/failed).
    disp = [f for k, f in rec.events if k == "task_disposition" and f["task_id"] == "B"]
    assert disp and disp[0]["disposition"] == "cap_exceeded"
    # parent woken with the cap_exceeded disposition in the payload.
    kind, payload = reg.inbox_of("alice", "a2a", "sP")[0]
    assert kind == "task_dependency_aborted"
    assert payload["meta"]["disposition"] == "cap_exceeded"
    assert "cap_exceeded" in payload["text"]


@pytest.mark.asyncio
async def test_cap_hit_at_exact_cap_terminates():
    """Tier 2: hitting the cap exactly (cost_accum == budget_cap) terminates."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    await b.create(_task("t", budget_cap=5.0, assignee="a2a:st", parent_id=None,
                         status=TaskState.IN_PROGRESS))
    res = await taskmod.record_task_cost(_ctx(b, TaskWaker(reg, "alice")), "t", 5.0)
    assert res.status is TaskState.ARCHIVED  # >= cap


# ── the cap-hit recovery loop (end-to-end mechanism) ────────────────────────


@pytest.mark.asyncio
async def test_cap_hit_recovery_loop_parent_removes_dep_readies_dependent():
    """Tier 2: cap-hit → the parent is woken (cap_exceeded) → it removes the dead
    dependency from the stuck dependent → the dependent (its last dep gone) becomes
    ready AND is woken (the recovery loop closes through the SAME seam as abort)."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    waker = TaskWaker(reg, "alice")
    await b.create(_task("P", assignee="a2a:sP", status=TaskState.IN_PROGRESS))
    await b.create(_task("B", budget_cap=5.0, assignee="a2a:sB", parent_id="P",
                         status=TaskState.IN_PROGRESS))
    await b.create(_task("A", deps=["B"], assignee="a2a:sA", parent_id="P"))  # blocked on B
    ctx = _ctx(b, waker)

    # 1. B hits its cap → the parent P is woken with cap_exceeded.
    await taskmod.record_task_cost(ctx, "B", 6.0)
    assert reg.inbox_of("alice", "a2a", "sP")[0][1]["meta"]["disposition"] == "cap_exceeded"

    # 2. the parent's recovery move: drop A's dead dependency on B → A (last dep
    #    gone, I-1) becomes ready and is woken.
    await taskmod._remove_dependency(SimpleNamespace(task_id="A", depends_on="B"),
                                     ctx, "control_ir")
    assert (await b.get("A")).status is TaskState.READY
    assert reg.inbox_of("alice", "a2a", "sA")[0][0] == "task_ready"
