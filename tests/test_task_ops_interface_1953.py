"""Tier 1/2: #1953 slice 1 — Task op interface contract surface.

The ``task.*`` Control IR ops exist, validate through the ControlIROp union, are
registered + gated (completeness), and round-trip through the in-memory backend.
Enforcement (single-writer CAS, abort quiescence, cascade, cycle-check,
predicate-eval) lands in later slices — this slice is the contract surface.

Falsification:
- completeness test reds if any task op kind is missing from the contextual gate
  (a silent capability bypass) or from the handler registry.
- the writer-token test reds if ``update_status`` stops threading the caller's
  run_id as the single-writer claim token (audit C2).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from reyn.core.op_runtime import available_kinds
from reyn.core.op_runtime import task as taskmod
from reyn.core.op_runtime.contextual_gate import _OP_KIND_ALIASES
from reyn.core.op_runtime.registry import ALL_OP_KINDS, OP_KIND_MODEL_MAP, OP_PURITY
from reyn.schemas.models import ControlIROp
from reyn.task import InMemoryTaskBackend, Task, TaskState

_TASK_KINDS = frozenset(k for k in ALL_OP_KINDS if k.startswith("task."))


def _ctx(run_id: str = "run-1", agent_id: str = "alice"):
    """Minimal OpContext stand-in — the task handlers read only run_id + agent_id."""
    return SimpleNamespace(run_id=run_id, agent_id=agent_id)


@pytest.fixture(autouse=True)
def _reset_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


# ── registry / gate completeness (Tier 1 contract) ──────────────────────────


def test_all_task_kinds_present_in_registry_and_purity():
    """Tier 1: every task op kind has a model + purity + handler (no half-wiring)."""
    assert _TASK_KINDS  # non-empty; the exact set is pinned in the union test
    handlers = set(available_kinds())
    for kind in _TASK_KINDS:
        assert kind in OP_KIND_MODEL_MAP
        assert kind in OP_PURITY
        assert kind in handlers


def test_contextual_gate_covers_every_task_kind():
    """Tier 1: the contextual gate enumerates every task kind — a missing entry
    would be a silent capability bypass (the #1912b completeness invariant)."""
    missing = _TASK_KINDS - set(_OP_KIND_ALIASES)
    # RED if a task op is added without a gate entry.
    assert missing == set()


def test_union_validates_every_task_kind():
    """Tier 1: each task op kind round-trips through the ControlIROp union."""
    adapter = TypeAdapter(ControlIROp)
    samples = {
        "task.create": {"kind": "task.create", "name": "n", "assignee": "a", "requester": "r"},
        "task.update_status": {"kind": "task.update_status", "task_id": "t", "status": "in_progress"},
        "task.get": {"kind": "task.get", "task_id": "t"},
        "task.list": {"kind": "task.list"},
        "task.create_subtask": {"kind": "task.create_subtask", "parent_id": "p", "name": "n", "assignee": "b"},
        "task.add_dependency": {"kind": "task.add_dependency", "task_id": "t", "depends_on": "u"},
        "task.abort": {"kind": "task.abort", "task_id": "t"},
        "task.archive": {"kind": "task.archive", "task_id": "t"},
        "task.heartbeat": {"kind": "task.heartbeat", "task_id": "t"},
        "task.register_unblock_predicate": {"kind": "task.register_unblock_predicate", "task_id": "t", "predicate": "x"},
        "task.comment": {"kind": "task.comment", "task_id": "t", "body": "hi"},
    }
    # every kind has a sample (forces this test to grow with the op-set)
    assert set(samples) == _TASK_KINDS
    for kind, payload in samples.items():
        op = adapter.validate_python(payload)
        assert op.kind == kind


# ── backend round-trip (Tier 2 — in-memory stub) ────────────────────────────


@pytest.mark.asyncio
async def test_inmemory_backend_create_get_list_roundtrip():
    """Tier 2: a non-default Task round-trips through the in-memory backend."""
    backend = InMemoryTaskBackend()
    task = Task(
        task_id="t1", name="ship", assignee="bob", requester="alice",
        status=TaskState.BLOCKED, budget_cap=12.5,
    )
    await backend.create(task)

    got = await backend.get("t1")
    assert got is not None
    assert got.assignee == "bob" and got.budget_cap == 12.5
    assert got.status is TaskState.BLOCKED

    by_assignee = await backend.list(assignee="bob")
    assert [t.task_id for t in by_assignee] == ["t1"]
    assert await backend.list(assignee="nobody") == []


# ── handler contract (Tier 2) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_then_get_via_handlers():
    """Tier 2: task.create returns a task_id that task.get resolves."""
    created = await taskmod._create(
        SimpleNamespace(name="ship", assignee="bob", requester="alice",
                        origin="self", description=None, budget_cap=None, deps=[]),
        _ctx(), "control_ir",
    )
    assert created["status"] == "ok"
    task_id = created["task"]["task_id"]

    got = await taskmod._get(SimpleNamespace(task_id=task_id), _ctx(), "control_ir")
    assert got["status"] == "ok"
    assert got["task"]["assignee"] == "bob"


@pytest.mark.asyncio
async def test_update_status_threads_run_id_as_writer_token():
    """Tier 2: update_status threads the caller's run_id as the single-writer
    claim token (audit C2) — the backend records it on first write."""
    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="bob", requester="alice",
                        origin="self", description=None, budget_cap=None, deps=[]),
        _ctx(run_id="run-XYZ"), "control_ir",
    )
    task_id = created["task"]["task_id"]

    updated = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="in_progress", reason=None),
        _ctx(run_id="run-XYZ"), "control_ir",
    )
    assert updated["status"] == "ok"
    # RED if update_status stops threading run_id as the claim token (C2 fix gone).
    assert updated["task"]["current_run_id"] == "run-XYZ"
    assert updated["task"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_archive_and_abort_reach_terminal_states():
    """Tier 2: archive → archived, abort → aborted (terminal contract surface)."""
    async def _mk(name):
        c = await taskmod._create(
            SimpleNamespace(name=name, assignee="b", requester="a",
                            origin="self", description=None, budget_cap=None, deps=[]),
            _ctx(), "control_ir",
        )
        return c["task"]["task_id"]

    a_id = await _mk("a")
    b_id = await _mk("b")
    arch = await taskmod._archive(SimpleNamespace(task_id=a_id, reason=None), _ctx(), "control_ir")
    abrt = await taskmod._abort(SimpleNamespace(task_id=b_id, reason="cancel"), _ctx(), "control_ir")
    assert arch["task"]["status"] == "archived"
    assert abrt["task"]["status"] == "aborted"


@pytest.mark.asyncio
async def test_handlers_return_error_for_unknown_task():
    """Tier 2: ops on a missing task return a decision-enabling error, not a crash."""
    got = await taskmod._get(SimpleNamespace(task_id="nope"), _ctx(), "control_ir")
    assert got["status"] == "error"
    assert "not found" in got["error"]
