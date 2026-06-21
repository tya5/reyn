"""Tier 2: #1981 — RunEntry→Task migration (the two coherence gaps + P3 reflect).

Completes the slice-5b "Task = single A2A authority" model for the three paths
that still read/wrote RunEntry directly:

  - **P2 escalation** — `_escalate_to_task` now creates the canonical Task (via
    the shared create-path), so an escalated run resolves via GetTask (was 404).
  - **P1 SSE** — the `/events` terminal decision reads the **Task** authority, so
    an A2A Cancel (which archives the Task without touching `RunEntry.status`)
    closes the stream (it would otherwise hang forever).
  - **P3 reflect** — an ask_user dispatch reflects the Task → `blocked`, and an
    answer reflects it → `in_progress`, keeping GetTask coherent with the
    RunEntry input-required mirror. The iv resolution itself stays Session-owned
    (#292 α).

Real backends + real Session (no mocks).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_WORKTREE_SRC = Path(__file__).parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")

import reyn.interfaces.web.routers.a2a as a2a_mod  # noqa: E402
from reyn.interfaces.web.a2a_intervention import A2AInterventionBus  # noqa: E402
from reyn.interfaces.web.run_registry import RunRegistry  # noqa: E402
from reyn.runtime.a2a_routing import a2a_session_id  # noqa: E402
from reyn.task import InMemoryTaskBackend, Task, TaskOrigin, TaskState  # noqa: E402
from reyn.user_intervention import UserIntervention  # noqa: E402


def _a2a_task(task_id, ctx, status=TaskState.IN_PROGRESS):
    return Task(task_id=task_id, name="n", assignee=a2a_session_id(ctx),
                requester="external", origin=TaskOrigin.EXTERNAL, status=status)


# ── P2: escalation creates the canonical Task (GetTask 404 → resolves) ───────


@pytest.mark.asyncio
async def test_escalation_creates_canonical_task(monkeypatch):
    """Tier 2: #1981 P2 — `_escalate_to_task` creates the canonical Task via the
    shared create-path, so the escalated run is GetTask-resolvable (the post-5a-1
    404 gap). RED if escalation drops the Task creation."""
    async def _no_session(*_a, **_k):
        return None
    # Keep the spawned monitor benign (no real session) — it fails fast + is caught.
    monkeypatch.setattr(a2a_mod, "_get_session_for_monitor", _no_session)

    rr = RunRegistry()
    tb = InMemoryTaskBackend()
    res = await a2a_mod._escalate_to_task(
        1, "alice", ["skill-1"], object(), rr,
        context_id="ctx-esc", task_backend=tb,
    )
    run_id = res["result"]["id"]

    # GAP FIX: the escalated run now has a canonical Task (was None → GetTask 404).
    task = await tb.get(run_id)
    assert task is not None
    assert task.assignee == a2a_session_id("ctx-esc")  # the per-contextId session
    assert task.origin is TaskOrigin.EXTERNAL
    # tidy the benign monitor task
    entry = rr.get(run_id)
    if entry is not None and entry.task is not None:
        entry.task.cancel()


# ── P1: the SSE terminal decision reads the Task authority ──────────────────


def _sse_client(run_registry, task_backend):
    from fastapi.testclient import TestClient

    from reyn.interfaces.web.deps import get_run_registry, get_task_backend
    from reyn.interfaces.web.server import app

    app.dependency_overrides[get_run_registry] = lambda: run_registry
    app.dependency_overrides[get_task_backend] = lambda: task_backend
    return TestClient(app, raise_server_exceptions=False)


def test_sse_closes_when_task_is_terminal_even_if_runentry_running():
    """Tier 2: #1981 P1 — an archived Task (A2A Cancel) closes the SSE stream even
    though `RunEntry.status` is still "running" (Cancel never touches it). Reading
    the RunEntry alone (pre-#1981) would loop forever; the test completing proves
    the Task authority is consulted."""
    from reyn.interfaces.web.server import app

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="c-sse")
    registry.append_event(entry.run_id, {"type": "progress", "msg": "working"})
    # RunEntry stays "running" (an A2A Cancel does NOT update it).
    assert registry.get(entry.run_id).status == "running"

    backend = InMemoryTaskBackend()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        backend.create(_a2a_task(entry.run_id, "ctx-sse", status=TaskState.ARCHIVED)))

    client = _sse_client(registry, backend)
    try:
        r = client.get(f"/a2a/tasks/{entry.run_id}/events")
        assert r.status_code == 200, r.text
        assert '"progress"' in r.text          # replayed the buffered event
        assert "event: end" in r.text          # then closed via the Task terminal
    finally:
        app.dependency_overrides.clear()


def test_sse_not_found_for_unknown_run():
    """Tier 2: an unknown run still yields the SSE not_found error (unchanged)."""
    from reyn.interfaces.web.server import app

    client = _sse_client(RunRegistry(), InMemoryTaskBackend())
    try:
        r = client.get("/a2a/tasks/no-such/events")
        assert r.status_code == 200
        assert "not_found" in r.text
    finally:
        app.dependency_overrides.clear()


# ── P3: ask_user dispatch reflects Task → blocked ───────────────────────────


@pytest.mark.asyncio
async def test_iv_dispatch_reflects_task_blocked():
    """Tier 2: #1981 P3 — an ask_user dispatch reflects the canonical Task →
    blocked (= A2A input-required), via the assignee CAS. RED if the bus drops the
    Task reflection (GetTask would show working during an ask_user)."""
    rr = RunRegistry()
    entry = rr.create(agent_name="alice", chain_id="c-iv", session_id=a2a_session_id("ctx-iv2"))
    tb = InMemoryTaskBackend()
    await tb.create(_a2a_task(entry.run_id, "ctx-iv2"))

    bus = A2AInterventionBus(entry.run_id, rr, task_backend=tb)
    await bus.on_dispatch(UserIntervention(kind="ask_user", prompt="?", run_id=entry.run_id))

    task = await tb.get(entry.run_id)
    assert task.status is TaskState.BLOCKED
    # the RunEntry input-required mirror still fires (unchanged).
    assert rr.get(entry.run_id).status == "input-required"


@pytest.mark.asyncio
async def test_iv_dispatch_without_task_backend_is_noop():
    """Tier 2: a bus with no Task backend still mirrors the RunEntry (the Task
    reflection is opt-in / best-effort — never raises)."""
    rr = RunRegistry()
    entry = rr.create(agent_name="alice", chain_id="c-iv3", session_id=a2a_session_id("ctx-iv3"))
    bus = A2AInterventionBus(entry.run_id, rr)  # no task_backend
    await bus.on_dispatch(UserIntervention(kind="ask_user", prompt="?", run_id=entry.run_id))
    assert rr.get(entry.run_id).status == "input-required"


# ── P3: answer reflects Task → in_progress (mechanism) ──────────────────────


@pytest.mark.asyncio
async def test_reflect_blocked_task_to_in_progress():
    """Tier 2: #1981 P3 — the reflection helper moves a blocked Task →
    in_progress on answer (the assignee's own status write). RED if the blocked→
    in_progress reflection is dropped (GetTask would stay input-required)."""
    tb = InMemoryTaskBackend()
    await tb.create(_a2a_task("t-ans", "ctx-ans", status=TaskState.BLOCKED))

    await a2a_mod._reflect_task_status(tb, "t-ans", "in_progress")

    assert (await tb.get("t-ans")).status is TaskState.IN_PROGRESS


@pytest.mark.asyncio
async def test_reflect_status_on_terminal_task_is_swallowed():
    """Tier 2: reflecting onto an already-archived (cancelled) Task is rejected by
    the terminal-guard and swallowed — the abort wins (race-safe)."""
    tb = InMemoryTaskBackend()
    await tb.create(_a2a_task("t-term", "ctx-t", status=TaskState.IN_PROGRESS))
    await tb.abort("t-term")  # A2A Cancel archived it first

    # Must not raise even though the Task is terminal.
    await a2a_mod._reflect_task_status(tb, "t-term", "blocked")
    assert (await tb.get("t-term")).status is TaskState.ARCHIVED
