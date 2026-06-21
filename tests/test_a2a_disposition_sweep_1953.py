"""Tier 2: #1953 slice 5a-2 — A2A disposition sweep (backend-derived webhook).

The periodic sweep notifies the external requester of every archived
``origin=external`` Task via its registered webhook, exactly once, retrying
failures, bounded by construction. Real Task backend + a real recording poster
(no mocks). The webhook channel map is A2A-owned (P7 — never on the Task).

Falsification:
- (a) an archived external task with a registered webhook → fires once;
- (b) an archived self-origin task → never fires;
- (c) a second sweep pass → no re-fire (notified-set);
- (d) a failed POST → not notified → retried next sweep;
- (e) the notified-set self-prunes hard-deleted task_ids (bounded);
- (f) the registry persists the map + notified-set across a reload.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.interfaces.web.a2a_webhook_registry import (
    A2AWebhookRegistry,
    sweep_dispositions,
)
from reyn.runtime.a2a_routing import a2a_session_id
from reyn.task import InMemoryTaskBackend, Task, TaskOrigin, TaskState


class _RecordingPoster:
    """A real injectable webhook poster (not a mock) that records calls."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ok = ok

    async def __call__(self, url: str, payload: dict):
        self.calls.append((url, payload))
        return SimpleNamespace(ok=self._ok)


async def _archived(backend, task_id, ctx, origin):
    """Seed an archived task assigned to the context's session."""
    await backend.create(Task(task_id=task_id, name="n", assignee=a2a_session_id(ctx),
                              requester="ext", origin=origin, status=TaskState.ARCHIVED))


@pytest.mark.asyncio
async def test_sweep_fires_for_external_and_not_for_self():
    """Tier 2: an archived external task with a registered webhook fires (a);
    an archived self-origin task never fires (b)."""
    backend = InMemoryTaskBackend()
    await _archived(backend, "ext-1", "ctx-a", TaskOrigin.EXTERNAL)
    await _archived(backend, "self-1", "ctx-b", TaskOrigin.SELF)
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    reg.register_webhook("ctx-b", "https://client.example/hook")  # self still excluded
    poster = _RecordingPoster()

    fired = await sweep_dispositions(backend, reg, post_fn=poster)

    # RED if origin filter drops (self would fire) or the external one is missed.
    assert fired == 1
    assert [c[1]["task_id"] for c in poster.calls] == ["ext-1"]
    assert poster.calls[0][0] == "https://client.example/hook"
    assert poster.calls[0][1]["contextId"] == "ctx-a"
    assert reg.is_notified("ext-1") and not reg.is_notified("self-1")


@pytest.mark.asyncio
async def test_sweep_does_not_refire_on_second_pass():
    """Tier 2: (c): a notified task is not re-fired on the next sweep."""
    backend = InMemoryTaskBackend()
    await _archived(backend, "ext-1", "ctx-a", TaskOrigin.EXTERNAL)
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    poster = _RecordingPoster()

    await sweep_dispositions(backend, reg, post_fn=poster)
    await sweep_dispositions(backend, reg, post_fn=poster)

    # RED if the notified-set is dropped: the task would re-fire (the list would
    # carry "ext-1" twice).
    assert [c[1]["task_id"] for c in poster.calls] == ["ext-1"]


@pytest.mark.asyncio
async def test_sweep_retries_failed_post():
    """Tier 2: (d): a failed POST leaves the task un-notified → retried next sweep
    (§24 forward-progress with retry)."""
    backend = InMemoryTaskBackend()
    await _archived(backend, "ext-1", "ctx-a", TaskOrigin.EXTERNAL)
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")

    failing = _RecordingPoster(ok=False)
    await sweep_dispositions(backend, reg, post_fn=failing)
    assert not reg.is_notified("ext-1")  # not marked on failure

    ok_poster = _RecordingPoster(ok=True)
    await sweep_dispositions(backend, reg, post_fn=ok_poster)
    assert ok_poster.calls and reg.is_notified("ext-1")  # retried + succeeded


@pytest.mark.asyncio
async def test_sweep_skips_context_without_registered_webhook():
    """Tier 2: an external task whose context has no webhook (e.g. pre-5b) is
    skipped (no channel) — no crash, no notify."""
    backend = InMemoryTaskBackend()
    await _archived(backend, "ext-1", "ctx-a", TaskOrigin.EXTERNAL)
    reg = A2AWebhookRegistry()  # no webhook registered
    poster = _RecordingPoster()

    fired = await sweep_dispositions(backend, reg, post_fn=poster)
    assert fired == 0 and poster.calls == []


@pytest.mark.asyncio
async def test_reconcile_prunes_hard_deleted_notified():
    """Tier 2: (e): the notified-set self-prunes a task_id no longer present
    (hard-deleted) — bounded by construction."""
    backend = InMemoryTaskBackend()
    await _archived(backend, "ext-1", "ctx-a", TaskOrigin.EXTERNAL)
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    reg.mark_notified("gone-task")  # a task no longer in the backend
    reg.mark_notified("ext-1")

    await sweep_dispositions(backend, reg, post_fn=_RecordingPoster())

    # RED if reconcile is dropped: the notified-set grows unbounded with stale ids.
    assert not reg.is_notified("gone-task")
    assert reg.is_notified("ext-1")  # still present → kept


def test_registry_persists_map_and_notified_across_reload(tmp_path):
    """Tier 2: (f): the contextId→webhook map + notified-set survive a reload (so
    a server restart neither loses a pending webhook nor re-fires a delivered one)."""
    path = tmp_path / "state" / "a2a_webhooks.json"
    reg = A2AWebhookRegistry(persist_path=path)
    reg.register_webhook("ctx-a", "https://client.example/hook")
    reg.mark_notified("ext-1")

    reloaded = A2AWebhookRegistry(persist_path=path)
    assert reloaded.webhook_for("ctx-a") == "https://client.example/hook"
    assert reloaded.is_notified("ext-1")
