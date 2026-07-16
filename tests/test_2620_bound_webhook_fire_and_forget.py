"""Tier 2b: #2620 — ``reyn.hooks.external_fire.fire_and_forget`` bounds the
number of concurrently-queued external-event dispatches PER SESSION rather
than scheduling an unbounded ``asyncio.create_task`` per fire.

Before #2620, ``fire_and_forget`` scheduled one background
``asyncio.create_task`` per call with no bound at all — a webhook flood
(H5's out-of-process, semi-trusted ingress path) could spawn arbitrarily
many concurrent hook-dispatch tasks. #2620 gives each session its own
bounded dispatch bridge (mirroring ``reyn.hooks.ingress._BoundedEventBridge``,
the shape H1/H4 already use for their in-process ingress): a fixed-size
``asyncio.Queue`` drained by one sequential background task, drop-newest
and log on overflow.

Real instances only (testing policy): a real ``Session`` (no registry
needed — ``dispatch_external_event`` is a plain method on Session), the
REAL ``WebhookIngressAdapter``/``dispatch_webhook_received`` production
call path (not a hand-rolled call to ``fire_and_forget``), and the session's
own public ``inbox`` to observe how many hook dispatches actually landed.
No ``unittest.mock``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks import external_fire
from reyn.runtime.session import Session


def _make_session(tmp_path: Path) -> Session:
    hooks_config = [
        {"on": "webhook_received", "template_push": {"message": "hit from {{ sender }}"}},
    ]
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return Session(agent_name="bound_test_agent", state_log=state_log, hooks_config=hooks_config)


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.02) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


def _drain_hook_texts(session: Session) -> list[str]:
    items = []
    while not session.inbox.empty():
        items.append(session.inbox.get_nowait())
    return [payload["text"] for kind, payload in items if kind == "hook"]


@pytest.mark.asyncio
async def test_fire_and_forget_drops_newest_beyond_bound_not_unbounded_tasks(tmp_path):
    """Tier 2b: the CORE #2620 proof. Fire N+3 real ``webhook_received``
    events (via the real ``WebhookIngressAdapter``/``fire_and_forget`` path,
    not the raw ``asyncio.create_task`` production used pre-#2620) in a
    tight loop with NO ``await`` in between — since the drain task cannot
    run until this coroutine yields, this places every fire ahead of the
    boundary N (queue maxsize) at the moment it is submitted, not merely
    "used a bounded queue so it must be bounded". The queue must actually
    fill and actually drop the overflow (observed via the public
    ``dropped_dispatch_count``/``pending_dispatch_count`` snapshot reads —
    never private bridge state)."""
    maxsize = 4
    session = _make_session(tmp_path)

    from reyn.hooks.ingress import WebhookIngressAdapter

    adapter = WebhookIngressAdapter()
    total_fires = maxsize + 3  # strictly outside the boundary, not merely at it
    for i in range(total_fires):
        event = adapter.to_event(f"webhook:sender-{i}")
        # Exercise the REAL production entry point with the test's small
        # bound instead of the 32-default, so the boundary is crossed
        # within a fast unit test.
        external_fire.fire_and_forget(
            session, "webhook_received", event.payload, maxsize=maxsize,
        )

    # Before the drain task gets a chance to run (no await yet in THIS
    # coroutine), every one of total_fires calls already resolved to either
    # "queued" or "dropped" synchronously inside fire_and_forget.
    assert external_fire.pending_dispatch_count(session) == maxsize
    assert external_fire.dropped_dispatch_count(session) == total_fires - maxsize

    # Let the drain task actually process the (bounded) backlog.
    await _wait_for(lambda: external_fire.pending_dispatch_count(session) == 0)
    await asyncio.sleep(0.05)  # let the last dispatch's hook push land in the inbox

    hook_texts = _drain_hook_texts(session)
    # Exactly `maxsize` hooks actually dispatched — the dropped overflow
    # never reached HookDispatcher at all (bounded by construction, not by
    # HookDispatcher happening to be fast).
    assert len(hook_texts) == maxsize


@pytest.mark.asyncio
async def test_fire_and_forget_at_the_boundary_drops_nothing(tmp_path):
    """Tier 2b: negative control for the bound test above — exactly maxsize
    fires (AT, not beyond, the boundary) drop nothing. Without this, the
    prior test alone couldn't distinguish "bounded at N" from "bounded at
    some smaller N' that also happens to make N+3 overflow"."""
    maxsize = 4
    session = _make_session(tmp_path)

    from reyn.hooks.ingress import WebhookIngressAdapter

    adapter = WebhookIngressAdapter()
    for i in range(maxsize):
        event = adapter.to_event(f"webhook:sender-{i}")
        external_fire.fire_and_forget(
            session, "webhook_received", event.payload, maxsize=maxsize,
        )

    assert external_fire.pending_dispatch_count(session) == maxsize
    assert external_fire.dropped_dispatch_count(session) == 0

    await _wait_for(lambda: external_fire.pending_dispatch_count(session) == 0)
    await asyncio.sleep(0.05)
    assert len(_drain_hook_texts(session)) == maxsize


@pytest.mark.asyncio
async def test_fire_and_forget_reuses_one_bridge_per_session(tmp_path):
    """Tier 2b: two separate fires for the SAME session share one bounded
    bridge (not a fresh queue/task per call — that would silently defeat
    the bound by giving every call its own private maxsize slots)."""
    maxsize = 2
    session = _make_session(tmp_path)

    from reyn.hooks.ingress import WebhookIngressAdapter

    adapter = WebhookIngressAdapter()
    # Fill the bound in one call...
    for i in range(maxsize):
        event = adapter.to_event(f"webhook:sender-{i}")
        external_fire.fire_and_forget(
            session, "webhook_received", event.payload, maxsize=maxsize,
        )
    assert external_fire.dropped_dispatch_count(session) == 0
    # ...then a THIRD call before the drain task runs must be dropped
    # against the SAME queue, not a brand-new one.
    event = adapter.to_event("webhook:sender-overflow")
    external_fire.fire_and_forget(session, "webhook_received", event.payload, maxsize=maxsize)
    assert external_fire.dropped_dispatch_count(session) == 1
