"""Tier 2: #1811 — A2A ``tasks/list`` (ListTasks) listing + filter + pagination.

``tasks/list`` returns a contextId's Tasks (the context's assignee session),
narrowable by ``status``, with a stable keyset cursor (sort key
``(created_at, task_id)``; opaque base64 ``pageToken``; ``nextPageToken`` MUST
always be present, ``''`` at end). Per-task shape is the spec A2A Task envelope
(``to_a2a_task``, #1953 slice 5a — Task-backed; replaces the #1947 RunRegistry
version). Real Task backend, no mocks.

Falsification:
- contextId filter test reds if the session_id filter is dropped (returns both
  contexts' tasks).
- pagination test reds if ``nextPageToken`` is never emitted (single page) or if
  pages overlap / skip entries.
- always-present test reds if the key is omitted on the final (short) page.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.web.routers.a2a import _handle_tasks_list
from reyn.runtime.a2a_routing import a2a_session_id
from reyn.task import InMemoryTaskBackend, Task, TaskState

_SEED_COUNTER = [0]


async def _seed(backend: InMemoryTaskBackend, session_id: str, n: int, *,
                status: TaskState = TaskState.IN_PROGRESS):
    """Create ``n`` Tasks assigned to ``session_id`` (the contextId's session),
    with globally-unique task_ids + deterministic strictly-increasing created_at
    so the keyset order is fully known across multiple seed calls."""
    created = []
    for _ in range(n):
        k = _SEED_COUNTER[0]
        _SEED_COUNTER[0] += 1
        t = Task(task_id=f"task-{k}", name=f"n{k}", assignee=session_id,
                 requester="req", status=status,
                 created_at=f"2026-01-01T00:{k // 60:02d}:{k % 60:02d}+00:00")
        await backend.create(t)
        created.append(t)
    return created


async def _list(backend: InMemoryTaskBackend, agent: str, **params) -> dict:
    resp = await _handle_tasks_list(req_id="r1", params=params, agent_name=agent,
                                    task_backend=backend)
    assert resp["jsonrpc"] == "2.0"
    assert "result" in resp, resp
    return resp["result"]


@pytest.mark.asyncio
async def test_tasks_list_filters_by_contextid():
    """Tier 2: contextId scopes results to that context's session (assignee) only."""
    backend = InMemoryTaskBackend()
    ctx_a, ctx_b = "ctx-a", "ctx-b"
    a_ids = {t.task_id for t in await _seed(backend, a2a_session_id(ctx_a), 3)}
    await _seed(backend, a2a_session_id(ctx_b), 2)  # other context — must be excluded

    result = await _list(backend, "alice", contextId=ctx_a)
    returned = {t["id"] for t in result["tasks"]}

    # RED if the session_id filter is dropped: returned would also contain ctx_b.
    assert returned == a_ids
    assert result["totalSize"] == 3


@pytest.mark.asyncio
async def test_tasks_list_pagination_keyset_covers_all_once():
    """Tier 2: keyset pages partition the full set — no overlap, no skips, and
    nextPageToken is non-empty until the final page then ''."""
    backend = InMemoryTaskBackend()
    ctx = "ctx-pg"
    all_ids = {t.task_id for t in await _seed(backend, a2a_session_id(ctx), 5)}

    seen: list[str] = []
    token = ""
    pages = 0
    page_lens: list[int] = []
    while True:
        params = {"contextId": ctx, "pageSize": 2}
        if token:
            params["pageToken"] = token
        result = await _list(backend, "alice", **params)
        page_lens.append(len(result["tasks"]))
        seen.extend(t["id"] for t in result["tasks"])
        token = result["nextPageToken"]
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        if token == "":
            break

    # RED if nextPageToken never emitted (would be one page of 2, missing 3) or
    # if pages overlapped/skipped (seen != all_ids or duplicates).
    assert page_lens == [2, 2, 1]
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == all_ids
    assert result["totalSize"] == 5


@pytest.mark.asyncio
async def test_tasks_list_filters_by_status():
    """Tier 2: status narrows the set (matched against the Task-state vocab)."""
    backend = InMemoryTaskBackend()
    sid = a2a_session_id("ctx-st")
    done = {t.task_id for t in await _seed(backend, sid, 2, status=TaskState.COMPLETED)}
    await _seed(backend, sid, 3, status=TaskState.IN_PROGRESS)

    result = await _list(backend, "alice", contextId="ctx-st", status="completed")
    returned = {t["id"] for t in result["tasks"]}

    assert returned == done
    assert result["totalSize"] == 2


@pytest.mark.asyncio
async def test_tasks_list_next_page_token_always_present_on_short_page():
    """Tier 2: nextPageToken MUST be present and '' when results fit one page."""
    backend = InMemoryTaskBackend()
    seeded = await _seed(backend, a2a_session_id("ctx-1"), 1)

    result = await _list(backend, "alice", contextId="ctx-1")

    # RED if the key is omitted on a single short page.
    assert "nextPageToken" in result
    assert result["nextPageToken"] == ""
    # the single seeded task is the only one returned (no extra page expected).
    returned = {t["id"] for t in result["tasks"]}
    assert returned == {seeded[0].task_id}


@pytest.mark.asyncio
async def test_tasks_list_rejects_malformed_page_token():
    """Tier 2: a malformed pageToken is an invalid-params error, not a crash."""
    backend = InMemoryTaskBackend()
    await _seed(backend, a2a_session_id("ctx-1"), 1)

    resp = await _handle_tasks_list(
        req_id="r1",
        params={"contextId": "ctx-1", "pageToken": "!!!not-base64!!!"},
        agent_name="alice",
        task_backend=backend,
    )

    assert "error" in resp
    assert resp["error"]["code"] == -32602
