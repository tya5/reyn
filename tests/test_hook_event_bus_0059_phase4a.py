"""Tests for the Hook-Event Redesign Phase 4a — the per-Session Async
``HookBus`` (proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md``
§3.2/§3.3).

Coverage plan
-------------
Tier 1 (contract): ``HookBus`` — pub/sub broadcast, unit tested directly:
  every live subscriber observes the SAME published ``HookEvent`` instance
  (no consume-once semantics — N subscribers each get their own copy of the
  observation), a closed subscription stops receiving, and zero subscribers
  is a no-op (``publish`` never raises with none registered).
Tier 2 (OS invariant, dispatcher-unit): ``HookDispatcher.dispatch`` — driving
  the REAL dispatcher with an injected ``HookBus``:
  - Sync happy-path byte-identical: with ``bus=None`` (the default, matching
    every pre-Phase-4a call site), behavior is unchanged from the Phase 1-3
    suites (no bus attribute observably touched).
  - Independence: a Sync-registered hook's action AND the Bus broadcast both
    fire for the same dispatch (additive, not exclusive); a Bus-only
    subscriber (point with ZERO Sync hooks registered) still observes the
    event via the bus.
Tier 2 (OS invariant, Session-integration): a real ``Session``'s
  ``_hook_bus`` is per-instance — two independently-constructed sessions get
  two distinct ``HookBus`` objects, so a subscriber on session A's bus never
  observes an event dispatched on session B (§3.3 per-Session scope,
  structural isolation — no cross-session reference exists to observe
  through).
Strip-falsify: breaking ``HookBus.publish`` (skip the ``put_nowait`` — i.e.
  drop the subscriber notification) flips the broadcast test RED; restoring
  goes GREEN (documented inline in
  ``test_broadcast_reaches_every_subscriber_same_instance``'s docstring, the
  strip target for a reviewer to falsify against).

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_GET_TIMEOUT = 1.0  # bounds every subscription.get() await so a broken/dropped
# broadcast fails the assertion (RED) rather than hanging pytest forever.


async def _get(sub) -> HookEvent:
    return await asyncio.wait_for(sub.get(), timeout=_GET_TIMEOUT)

from reyn.core.events.state_log import StateLog
from reyn.hooks.bus import HookBus
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.event import HookEvent
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Recording seam (mirrors test_hook_dispatcher_1800_5b.py's _Recorder)
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable. Captures (args, kwargs) per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def kinds(self) -> list:
        """The first positional arg of each call (the inbox/stage kind)."""
        return [a[0] for (a, _k) in self.calls]


def _dispatcher(hooks: list[HookDef], *, bus: "HookBus | None" = None) -> tuple[HookDispatcher, dict]:
    seams = {
        "put_inbox": _Recorder(),
        "stage_next_turn_context": _Recorder(),
        "run_shell": _Recorder(),
    }
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
        bus=bus,
    )
    return disp, seams


# ---------------------------------------------------------------------------
# Tier 1: HookBus pub/sub broadcast — pure unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_reaches_every_subscriber_same_instance():
    """Tier 1: publish() delivers the SAME HookEvent instance to every live
    subscriber, simultaneously — broadcast, not a consume-once queue.

    Strip-falsify target: comment out the ``queue.put_nowait(event)`` line in
    ``HookBus.publish`` (or replace it with a no-op) — this assertion goes
    RED (``sub_a.get()``/``sub_b.get()`` never resolve); restoring the
    put_nowait call goes GREEN again.
    """
    bus = HookBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()
    event = HookEvent(kind="builtin:lifecycle:turn_end", payload={"chain_id": "c1"})

    bus.publish(event)

    got_a = await asyncio.wait_for(sub_a.get(), timeout=1.0)
    got_b = await asyncio.wait_for(sub_b.get(), timeout=1.0)
    assert got_a is event  # same instance, not a copy
    assert got_b is event
    assert got_a is got_b


@pytest.mark.asyncio
async def test_each_subscriber_independently_observes_every_event():
    """Tier 1: subscriber A reading its event does not consume it for B — each
    subscription owns its own queue (no shared consume-once state)."""
    bus = HookBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()
    e1 = HookEvent(kind="builtin:lifecycle:turn_start", payload={})
    e2 = HookEvent(kind="builtin:lifecycle:turn_end", payload={})

    bus.publish(e1)
    bus.publish(e2)

    # A drains both before B reads anything — B still sees both, in order.
    assert await _get(sub_a) is e1
    assert await _get(sub_a) is e2
    assert await _get(sub_b) is e1
    assert await _get(sub_b) is e2


@pytest.mark.asyncio
async def test_closed_subscription_stops_receiving():
    """Tier 1: closing a subscription detaches it — a later publish() is not
    delivered to it, and subscriber_count reflects the detach."""
    bus = HookBus()
    sub = bus.subscribe()
    assert bus.subscriber_count == 1

    sub.close()
    assert bus.subscriber_count == 0

    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={}))
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()


def test_publish_with_no_subscribers_is_a_noop():
    """Tier 1: publish() with zero live subscribers never raises and touches
    nothing (the no-subscriber byte-identical happy path, §3.2)."""
    bus = HookBus()
    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={}))  # must not raise
    assert bus.subscriber_count == 0


def _recorder():
    """A real recording callable (no MagicMock/patch, per testing policy) —
    used as a fake ``emit_event`` P6 sink."""
    calls: "list[tuple]" = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    return record, calls


def test_subscriber_queue_overflow_is_fail_visible():
    """Tier 1: (#2886) overflowing a subscriber's bounded queue is no longer
    silent — the drop increments that subscriber's ``snapshot_drop_counts()``
    entry, and the FIRST drop fires a metadata-only ``bus_subscriber_dropped``
    P6 audit-event through the ``emit_event`` sink (subscriber id + drop
    count only — never the dropped event's kind/payload).

    Strip-falsify: remove the ``self._audit_drop(state)`` call in
    ``HookBus.publish`` (or the ``state.drop_count += 1`` line) and this test
    goes RED — no ``bus_subscriber_dropped`` call / drop count stays 0.
    """
    emit_event, calls = _recorder()
    bus = HookBus(subscriber_maxsize=1, emit_event=emit_event)
    sub = bus.subscribe()

    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={"n": 0}))
    # Queue now holds 1 (its maxsize) unread event; the NEXT publish overflows
    # it and drops the oldest to make room.
    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={"n": 1}))

    counts = bus.snapshot_drop_counts()
    assert list(counts.values()) == [1]

    dropped_calls = [c for c in calls if c[0] and c[0][0] == "bus_subscriber_dropped"]
    (only_dropped_call,) = dropped_calls  # exactly one audit-event fired — unpack-must-flip
    (_args, kwargs) = only_dropped_call
    assert kwargs["drop_count"] == 1
    assert "subscriber_id" in kwargs
    # never the dropped event's content
    assert "kind" not in kwargs and "payload" not in kwargs

    sub.close()


def test_subscriber_queue_overflow_audits_first_then_every_nth_not_every_drop():
    """Tier 1: (#2886) under sustained overflow, the audit-event fires on the
    first drop and then only every Nth drop — NOT once per drop (publish is a
    sync/never-raises hot path; auditing every drop would flood the audit
    log under a slow-subscriber storm). ``snapshot_drop_counts()`` still
    counts every single drop regardless of audit cadence."""
    from reyn.hooks.bus import _AUDIT_EVERY_N_DROPS

    emit_event, calls = _recorder()
    bus = HookBus(subscriber_maxsize=1, emit_event=emit_event)
    bus.subscribe()

    total_publishes = _AUDIT_EVERY_N_DROPS + 2  # 1 fills the queue, the rest all drop
    for i in range(total_publishes):
        bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={"n": i}))

    expected_drops = total_publishes - 1
    assert list(bus.snapshot_drop_counts().values()) == [expected_drops]

    dropped_calls = [c for c in calls if c[0] and c[0][0] == "bus_subscriber_dropped"]
    # first drop (drop_count == 1) + the Nth drop (drop_count == _AUDIT_EVERY_N_DROPS) —
    # exactly two audit-events fired, unpack-must-flip if the cadence regresses.
    (first_call, nth_call) = dropped_calls
    assert first_call[1]["drop_count"] == 1
    assert nth_call[1]["drop_count"] == _AUDIT_EVERY_N_DROPS


def test_no_emit_event_sink_still_counts_drops_without_raising():
    """Tier 1: (#2886) ``emit_event`` is optional (default None, matching every
    other best-effort telemetry sink in this subsystem) — a drop with no sink
    wired still increments ``snapshot_drop_counts()`` and never raises."""
    bus = HookBus(subscriber_maxsize=1)
    bus.subscribe()

    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={}))
    bus.publish(HookEvent(kind="builtin:lifecycle:turn_end", payload={}))  # must not raise

    assert list(bus.snapshot_drop_counts().values()) == [1]


# ---------------------------------------------------------------------------
# Tier 2: HookDispatcher + injected HookBus — Sync happy-path + independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_with_bus_none_keeps_the_sync_hook_firing():
    """Tier 2: the default ``bus=None`` (every pre-Phase-4a call site) drives
    the exact same Sync behavior as the Phase 1-3 suites — a hook still fires,
    and no bus-shaped side effect exists to observe."""
    hook = HookDef(on="turn_end", template_push=PushBlock(message="continue", wake=True))
    disp, seams = _dispatcher([hook], bus=None)

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].kinds == ["hook"]


@pytest.mark.asyncio
async def test_sync_hook_and_bus_broadcast_both_fire_same_dispatch():
    """Tier 2: independence (§3.2) — a Sync-registered hook's action AND the
    Bus broadcast both happen for the SAME dispatch() call; neither
    suppresses the other."""
    bus = HookBus()
    sub = bus.subscribe()
    hook = HookDef(on="turn_end", template_push=PushBlock(message="continue", wake=True))
    disp, seams = _dispatcher([hook], bus=bus)

    await disp.dispatch("turn_end", {"chain_id": "c1"})

    # Sync side: the hook's push fired.
    assert seams["put_inbox"].kinds == ["hook"]
    # Bus side: the SAME dispatch also broadcast a HookEvent.
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event.kind == "builtin:lifecycle:turn_end"
    assert event.payload["chain_id"] == "c1"


@pytest.mark.asyncio
async def test_bus_only_subscriber_observes_event_with_zero_sync_hooks():
    """Tier 2: independence (§3.2) — a point with NO Sync hook registered
    still broadcasts to the Bus. Proves the Bus is not gated on
    ``hooks_for(point)`` being non-empty."""
    bus = HookBus()
    sub = bus.subscribe()
    disp, seams = _dispatcher([], bus=bus)  # empty registry — no Sync hook at all

    await disp.dispatch("session_start", {"agent_name": "a"})

    # No Sync side-effect (the no-hooks-equivalence property, unaffected).
    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []
    # But the Bus still observed the dispatch.
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event.kind == "builtin:lifecycle:session_start"


# ---------------------------------------------------------------------------
# Tier 2: real Session — per-Session Bus scope (§3.3)
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, name: str) -> Session:
    return Session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / f"{name}.wal"),
        snapshot_path=tmp_path / f"{name}.json",
        hooks_config=None,
    )


@pytest.mark.asyncio
async def test_per_session_bus_isolation(tmp_path):
    """Tier 2: §3.3 per-Session scope — two independently-constructed Sessions
    each get their OWN bus; a subscriber attached to session A's bus observes
    only events dispatched on A, never on B. Proven behaviorally (dispatch on
    A reaches A's subscriber but not B's, and vice versa) rather than by
    asserting the private ``_hook_bus`` attributes' identity directly."""
    session_a = _make_session(tmp_path, name="a")
    session_b = _make_session(tmp_path, name="b")

    sub_a = session_a._hook_bus.subscribe()
    sub_b = session_b._hook_bus.subscribe()

    await session_a._hook_dispatcher.dispatch("turn_end", {"chain_id": "from-a"})

    event_a = await asyncio.wait_for(sub_a.get(), timeout=1.0)
    assert event_a.payload["chain_id"] == "from-a"
    # B's subscription saw nothing from A's dispatch.
    with pytest.raises(asyncio.QueueEmpty):
        sub_b.get_nowait()

    await session_b._hook_dispatcher.dispatch("turn_end", {"chain_id": "from-b"})
    event_b = await asyncio.wait_for(sub_b.get(), timeout=1.0)
    assert event_b.payload["chain_id"] == "from-b"
    # A's subscription still only ever saw its own session's event.
    with pytest.raises(asyncio.QueueEmpty):
        sub_a.get_nowait()
