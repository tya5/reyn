"""Tier 2: #1811 — A2A ``tasks/list`` (ListTasks) listing + filter + pagination.

``tasks/list`` returns one agent's tasks, narrowable by ``contextId`` (→ the
core-neutral ``session_id`` routing-key, #1814) and ``status``, with a stable
keyset cursor (sort key ``(created_at, run_id)``; opaque base64 ``pageToken``;
``nextPageToken`` MUST always be present, ``''`` at end). Per-task shape reuses
``RunEntry.to_public_dict()`` (consistent with GetTask; STATUS_MAP normalization
is a separate #1811 slice). Real RunRegistry, no mocks.

Falsification:
- contextId filter test reds if the session_id filter is dropped (returns both
  contexts' tasks).
- pagination test reds if ``nextPageToken`` is never emitted (single page) or if
  pages overlap / skip entries.
- always-present test reds if the key is omitted on the final (short) page.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reyn.interfaces.web.routers.a2a import _handle_tasks_list
from reyn.interfaces.web.run_registry import RunRegistry
from reyn.runtime.a2a_routing import a2a_session_id


def _seed(reg: RunRegistry, agent: str, session_id: str, n: int, *, status: str = "running"):
    """Create ``n`` runs with deterministic, strictly-increasing created_at so
    the keyset order is fully known."""
    created = []
    for i in range(n):
        e = reg.create(agent_name=agent, chain_id=f"c{i}", session_id=session_id)
        e.status = status
        e.created_at = datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc)
        created.append(e)
    return created


async def _list(reg: RunRegistry, agent: str, **params) -> dict:
    resp = await _handle_tasks_list(req_id="r1", params=params, agent_name=agent, run_registry=reg)
    assert resp["jsonrpc"] == "2.0"
    assert "result" in resp, resp
    return resp["result"]


@pytest.mark.asyncio
async def test_tasks_list_filters_by_contextid():
    """Tier 2: contextId scopes results to that context's session_id only."""
    reg = RunRegistry()
    ctx_a, ctx_b = "ctx-a", "ctx-b"
    a_ids = {e.run_id for e in _seed(reg, "alice", a2a_session_id(ctx_a), 3)}
    _seed(reg, "alice", a2a_session_id(ctx_b), 2)  # other context — must be excluded

    result = await _list(reg, "alice", contextId=ctx_a)
    returned = {t["run_id"] for t in result["tasks"]}

    # RED if the session_id filter is dropped: returned would also contain ctx_b.
    assert returned == a_ids
    assert result["totalSize"] == 3


@pytest.mark.asyncio
async def test_tasks_list_pagination_keyset_covers_all_once():
    """Tier 2: keyset pages partition the full set — no overlap, no skips, and
    nextPageToken is non-empty until the final page then ''."""
    reg = RunRegistry()
    ctx = "ctx-pg"
    all_ids = {e.run_id for e in _seed(reg, "alice", a2a_session_id(ctx), 5)}

    seen: list[str] = []
    token = ""
    pages = 0
    page_lens: list[int] = []
    while True:
        params = {"contextId": ctx, "pageSize": 2}
        if token:
            params["pageToken"] = token
        result = await _list(reg, "alice", **params)
        page_lens.append(len(result["tasks"]))
        seen.extend(t["run_id"] for t in result["tasks"])
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
    """Tier 2: status narrows the set (matched against Reyn status names)."""
    reg = RunRegistry()
    sid = a2a_session_id("ctx-st")
    done = {e.run_id for e in _seed(reg, "alice", sid, 2, status="completed")}
    _seed(reg, "alice", sid, 3, status="running")

    result = await _list(reg, "alice", contextId="ctx-st", status="completed")
    returned = {t["run_id"] for t in result["tasks"]}

    assert returned == done
    assert result["totalSize"] == 2


@pytest.mark.asyncio
async def test_tasks_list_next_page_token_always_present_on_short_page():
    """Tier 2: nextPageToken MUST be present and '' when results fit one page."""
    reg = RunRegistry()
    seeded = _seed(reg, "alice", a2a_session_id("ctx-1"), 1)

    result = await _list(reg, "alice", contextId="ctx-1")

    # RED if the key is omitted on a single short page.
    assert "nextPageToken" in result
    assert result["nextPageToken"] == ""
    # the single seeded task is the only one returned (no extra page expected).
    returned = {t["run_id"] for t in result["tasks"]}
    assert returned == {seeded[0].run_id}


@pytest.mark.asyncio
async def test_tasks_list_rejects_malformed_page_token():
    """Tier 2: a malformed pageToken is an invalid-params error, not a crash."""
    reg = RunRegistry()
    _seed(reg, "alice", a2a_session_id("ctx-1"), 1)

    resp = await _handle_tasks_list(
        req_id="r1",
        params={"contextId": "ctx-1", "pageToken": "!!!not-base64!!!"},
        agent_name="alice",
        run_registry=reg,
    )

    assert "error" in resp
    assert resp["error"]["code"] == -32602
