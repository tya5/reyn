"""#2107 §16 S2 — the origin-split: an EXTERNAL-origin terminal's stuck dep-DAG
DEPENDENTS are aborted (no in-session recovery — the external requester gives up).
INTERNAL tasks keep the S1 requester-wake recovery (untouched).

The cascade lives in ONE seam — ``backend.abort`` (origin-gated EXTERNAL) — so EVERY
INTERNAL abort trigger still reachable after #2839 Phase 1 gets it by construction.
The trigger → seam table (proven below):

  | trigger                         | seam                                    |
  | agent abort op (_abort)         | backend.abort                           |
  | /tasks kill (slash)             | backend.abort (direct)                  |
  | assignee failed (_update_status)| _route_terminal_to_requester → backend.abort(deps) |

#2839 Phase 1 note: the fourth trigger this file originally proved — "A2A client
cancel (cancel_task) → backend.abort (direct)" — is retired. A2A's ``cancel_task``
endpoint now calls ``RunRegistry.cancel`` only (Phase 1 decouples A2A from the
internal Task backend entirely; see ``routers/a2a.py``). This is a deliberate,
scoped behavior change, not an oversight: an A2A-external Task acquiring real
dependents required some internal ``task.create(deps=[...])`` call to name that
specific run_id, and A2A never threads its own run_id into the driven session's
``OpContext.current_task_id`` (per #2839's Q3 investigation — an A2A run does not
go through the internal task-wake dispatch path at all), so the A2A-driven agent
has no way to reference its own run as a dep in the first place. The whole
cascade mechanism (including this file) is itself internal-Task-system surface
area slated for full removal in #2839 Phase 2/3, so this is a bring-forward, not
a net-new gap. The former "LIVE PATH" test proving the A2A-cancel trigger
specifically has been removed accordingly; the three tests below (all internal
Task-backend / op-layer triggers, untouched by Phase 1) remain valid.

Real backends (in-memory + sqlite); no mocks.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState
from reyn.task.model import TaskOrigin


def _ext(task_id, *, deps=None, status=TaskState.READY, assignee="a2a:ctx-1"):
    return Task(task_id=task_id, name=task_id, assignee=assignee, requester="a2a:ctx-1",
                origin=TaskOrigin.EXTERNAL, status=status, deps=list(deps or []))


def _self(task_id, *, deps=None, status=TaskState.READY):
    return Task(task_id=task_id, name=task_id, assignee="main", requester="main",
                origin=TaskOrigin.SELF, status=status, deps=list(deps or []))


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    if request.param == "inmem":
        yield InMemoryTaskBackend()
    else:
        b = SqliteTaskBackend(tmp_path / "s2.db")
        yield b
        b.close()


# ── the cascade: backend.abort on an EXTERNAL root aborts the transitive dependents ──


@pytest.mark.asyncio
async def test_external_abort_cascades_to_transitive_dependents(backend):
    """Tier 2: aborting an EXTERNAL task archives its TRANSITIVE dep-DAG dependents
    (X ← Y ← Z) — they can't be recovered, so they give up with it. Catches the direct
    backend.abort triggers (A2A cancel / agent abort / /tasks kill) by construction."""
    await backend.create(_ext("X", status=TaskState.RUNNING))
    await backend.create(_ext("Y", deps=["X"]))
    await backend.create(_ext("Z", deps=["Y"]))

    await backend.abort("X")

    for tid in ("X", "Y", "Z"):
        assert (await backend.get(tid)).status is TaskState.ABORTED


@pytest.mark.asyncio
async def test_internal_abort_does_not_cascade_to_dependents(backend):
    """Tier 2: origin-split — a SELF (internal) abort does NOT abort its dependents (they
    recover via the requester wake, §16 S1). Strip the origin gate → the dependent would
    be wrongly archived → RED."""
    await backend.create(_self("X", status=TaskState.RUNNING))
    await backend.create(_self("Y", deps=["X"]))

    await backend.abort("X")

    assert (await backend.get("X")).status is TaskState.ABORTED
    assert (await backend.get("Y")).status is not TaskState.ABORTED  # NOT cascaded


@pytest.mark.asyncio
async def test_external_failed_path_aborts_dependents_via_route(backend):
    """Tier 2: the failed trigger — an EXTERNAL task declared `failed` (NOT backend.abort'd
    on X itself) reaches _route_terminal_to_requester, whose EXTERNAL branch aborts X's
    stuck dependents (feeding the same backend.abort cascade). X stays FAILED; Y, Z archived."""
    await backend.create(_ext("X", status=TaskState.FAILED))
    await backend.create(_ext("Y", deps=["X"]))
    await backend.create(_ext("Z", deps=["Y"]))
    ctx = SimpleNamespace(task_backend=backend, session_id="a2a:ctx-1", agent_id="a",
                          events=None, task_waker=None)
    terminal = await backend.get("X")

    await taskmod._route_terminal_to_requester(ctx, backend, terminal, disposition="failed")

    assert (await backend.get("X")).status is TaskState.FAILED  # X itself stays failed
    assert (await backend.get("Y")).status is TaskState.ABORTED
    assert (await backend.get("Z")).status is TaskState.ABORTED
