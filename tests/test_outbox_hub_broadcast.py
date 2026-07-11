"""Tier 2: OutboxHub is a single-drain broadcast fan-out (ADR-0039 P6b).

``session.outbox`` is a single-consumer ``asyncio.Queue`` — each ``.get()`` hands
an item to exactly ONE getter, so two direct drainers (the local forwarder + an
AG-UI surface, or two AG-UI surfaces) *steal* frames from each other. The hub is
the sole ``.get()`` consumer and fans every message out to N per-surface
subscriptions. Two invariants, both proven here with real instances (a real
``asyncio.Queue`` source, a real ``OutboxHub``, real ``OutboxMessage``s — no
mocks):

1. **N>=2 (new capability):** two surfaces each receive the FULL stream in order.
   The strip (revert to per-connection ``source.get()``) splits it ~K/2 — proven
   directly by ``test_two_direct_getters_steal_*`` establishing the hazard the hub
   removes.
2. **N=1 (non-regression):** the hub is a transparent 1:1 pipe — delivery is
   byte-identical to a direct drain of the same script, so the pre-hub local path
   is preserved.

Plus the slow-surface policy: a bounded surface that stops draining is
disconnect-slow'd (its ``get()`` returns ``None``) while the writer never blocks
and other surfaces still receive the full stream.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import OutboxHub

_K = 20


def _script(k: int = _K) -> list[OutboxMessage]:
    return [OutboxMessage(kind="agent", text=str(i)) for i in range(k)]


async def _drain(sub) -> list[str]:
    """Drain a hub subscription to its terminal (``__end__`` or disconnect)."""
    got: list[str] = []
    while True:
        msg = await asyncio.wait_for(sub.get(), timeout=2.0)
        if msg is None or msg.kind == "__end__":
            return got
        got.append(msg.text)


@pytest.mark.asyncio
async def test_two_surfaces_each_receive_full_stream_in_order() -> None:
    """Tier 2: N>=2 — two hub surfaces BOTH receive every frame in order (no steal)."""
    source: asyncio.Queue = asyncio.Queue()
    hub = OutboxHub(source)
    a = hub.subscribe()
    b = hub.subscribe()
    for msg in _script():
        source.put_nowait(msg)
    source.put_nowait(OutboxMessage(kind="__end__", text=""))

    got_a, got_b = await asyncio.gather(_drain(a), _drain(b))

    expected = [str(i) for i in range(_K)]
    assert got_a == expected
    assert got_b == expected


@pytest.mark.asyncio
async def test_two_direct_getters_steal_the_stream() -> None:
    """Tier 2: the hazard the hub removes — two DIRECT ``source.get()`` drainers
    (the pre-hub per-connection drain) split the stream, neither sees it whole.

    This is the strip-falsify baseline made explicit: the hub's fan-out is what
    turns this ~K/2 split into two full streams (the test above)."""
    source: asyncio.Queue = asyncio.Queue()
    for msg in _script():
        source.put_nowait(msg)

    async def direct_drain() -> list[str]:
        got: list[str] = []
        for _ in range(_K):  # bounded so the losers of the race don't hang
            try:
                msg = await asyncio.wait_for(source.get(), timeout=0.2)
            except asyncio.TimeoutError:
                break
            got.append(msg.text)
        return got

    got_a, got_b = await asyncio.gather(direct_drain(), direct_drain())

    # Each item went to exactly ONE getter: a disjoint partition whose union is
    # the whole stream — so the two surfaces canNOT BOTH see it whole (the exact
    # negation of the hub invariant proven in the N>=2 test above).
    whole = [str(i) for i in range(_K)]
    assert set(got_a + got_b) == set(whole)
    assert set(got_a).isdisjoint(got_b)
    assert not (got_a == whole and got_b == whole)


@pytest.mark.asyncio
async def test_single_surface_is_transparent_pipe() -> None:
    """Tier 2: N=1 non-regression — the hub delivers exactly the source sequence,
    byte-identical to a direct drain of the same script (pre-hub path preserved)."""
    script = _script()

    # Direct-drain baseline (the pre-hub path): one getter, the whole script.
    direct_source: asyncio.Queue = asyncio.Queue()
    for msg in script:
        direct_source.put_nowait(msg)
    direct_source.put_nowait(OutboxMessage(kind="__end__", text=""))
    baseline: list[str] = []
    while True:
        msg = await asyncio.wait_for(direct_source.get(), timeout=2.0)
        if msg.kind == "__end__":
            break
        baseline.append(msg.text)

    # Same script through the hub at N=1.
    hub_source: asyncio.Queue = asyncio.Queue()
    hub = OutboxHub(hub_source)
    sub = hub.subscribe()
    for msg in script:
        hub_source.put_nowait(msg)
    hub_source.put_nowait(OutboxMessage(kind="__end__", text=""))
    via_hub = await _drain(sub)

    assert via_hub == baseline
    assert baseline == [str(i) for i in range(_K)]  # sanity: non-trivial script


@pytest.mark.asyncio
async def test_slow_surface_disconnected_without_blocking_the_writer() -> None:
    """Tier 2: a bounded surface that stops draining is disconnect-slow'd (its
    ``get()`` returns ``None``) while a fast surface still receives the FULL
    stream and the source fully drains — the writer is never blocked."""
    source: asyncio.Queue = asyncio.Queue()
    hub = OutboxHub(source)
    fast = hub.subscribe()  # unbounded
    slow = hub.subscribe(maxsize=2)  # tiny cap, never drained → disconnect-slow
    k = 50
    for i in range(k):
        source.put_nowait(OutboxMessage(kind="agent", text=str(i)))
    source.put_nowait(OutboxMessage(kind="__end__", text=""))

    # Fast surface receives everything — proves the drain was never blocked by
    # the stuck slow surface (if it had blocked, fast would hang / be short).
    got_fast = await _drain(fast)
    assert got_fast == [str(i) for i in range(k)]

    # Slow surface was force-closed: its next read is the None disconnect signal.
    assert await asyncio.wait_for(slow.get(), timeout=2.0) is None


@pytest.mark.asyncio
async def test_end_terminal_fans_out_to_all_surfaces() -> None:
    """Tier 2: ``__end__`` reaches every attached surface (terminal fan-out)."""
    source: asyncio.Queue = asyncio.Queue()
    hub = OutboxHub(source)
    subs = [hub.subscribe() for _ in range(3)]
    source.put_nowait(OutboxMessage(kind="agent", text="only"))
    source.put_nowait(OutboxMessage(kind="__end__", text=""))

    results = await asyncio.gather(*(_drain(s) for s in subs))
    for got in results:
        assert got == ["only"]
