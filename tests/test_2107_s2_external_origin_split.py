"""#2107 §16 S2 — the origin-split: an EXTERNAL-origin terminal's stuck dep-DAG
DEPENDENTS are aborted (no in-session recovery — the external requester gives up), and
the existing webhook sweep propagates each archived dependent to the A2A client. INTERNAL
tasks keep the S1 requester-wake recovery (untouched).

The cascade lives in ONE seam — ``backend.abort`` (origin-gated EXTERNAL) — so EVERY abort
trigger gets it by construction. The trigger → seam table (all proven below):

  | trigger                         | seam                                    |
  | A2A client cancel (cancel_task) | backend.abort (direct)                  |
  | agent abort op (_abort)         | backend.abort                           |
  | /tasks kill (slash)             | backend.abort (direct)                  |
  | assignee failed (_update_status)| _route_terminal_to_requester → backend.abort(deps) |

Real backends (in-memory + sqlite) + the REAL cancel_task endpoint + the REAL sweep; no
mocks (a recording webhook poster).
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


# ── the LIVE path: the REAL cancel_task endpoint → archived → the REAL sweep → webhooks ──


@pytest.mark.asyncio
async def test_live_cancel_endpoint_cascades_then_sweep_fires_webhooks(tmp_path):
    """Tier 2: #2107 S2 LIVE PATH — the A2A client CANCELS an external task through the REAL
    cancel_task endpoint (the trigger that bypasses the op-layer). Its dependents archive
    (the backend.abort cascade), and the REAL disposition sweep then fires a webhook to the
    A2A client for EVERY archived dependent — not just the cancelled root. A logic-trace
    (op-layer only) would miss this; the live path proves the cancel → cascade → propagate
    chain end-to-end."""
    from reyn.interfaces.web.a2a_webhook_registry import A2AWebhookRegistry, sweep_dispositions
    from reyn.interfaces.web.routers.a2a import cancel_task

    backend = InMemoryTaskBackend()
    await backend.create(_ext("X", status=TaskState.RUNNING))
    await backend.create(_ext("Y", deps=["X"]))
    await backend.create(_ext("Z", deps=["Y"]))

    registry = A2AWebhookRegistry()
    registry.register_webhook("ctx-1", "https://client.example/hook")  # for a2a:ctx-1

    # the REAL cancel endpoint (the A2A client's remove-op) — direct backend.abort.
    await cancel_task("X", task_backend=backend)
    for tid in ("X", "Y", "Z"):
        assert (await backend.get(tid)).status is TaskState.ABORTED  # the cascade

    posted: list[dict] = []

    async def _record_post(url, payload):
        posted.append({"url": url, **payload})
        return SimpleNamespace(ok=True)

    fired = await sweep_dispositions(backend, registry, post_fn=_record_post)

    # the sweep propagated EVERY archived dependent to the client (root + cascade), not 1.
    notified_ids = {p["task_id"] for p in posted}
    assert {"X", "Y", "Z"} <= notified_ids
    assert fired >= 3
    assert all(p["disposition"] == "aborted" for p in posted)
