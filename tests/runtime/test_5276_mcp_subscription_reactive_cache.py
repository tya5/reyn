"""Tier 2: #5276/#5287 — ``Session.mcp_subscription_state()``'s reactive cache.

Root cause: this method used to forward straight to
``MCPConnectionService.subscription_summary()`` on EVERY call, including
every render frame the status panel drew. That composition is real work
(iterates every held server, per-URI honored-set lookups) for a value that
only ever changes on a genuine subscription-relevant mutation.

#5276's original fix (a subscriber registered in ``Session.__init__``
marking the cache dirty when one of a hand-picked list of EventLog event
KINDS fired) needed a 7th kind added after shipping (#5280,
``mcp_reconnect_failed`` — a failed reconnect drops a server from
``held_servers()`` without firing any of the original 6) — a defect class
found to recur across this file's 2 sibling reactive caches too
(``hook_state``/``capability_visibility_state``'s envelope census).

#5287: the cache is now PULL-based against
``MCPConnectionService.generation`` — a producer-owned counter that class
bumps at every one of ITS OWN mutation sites (see
``MCPConnectionService._bump_generation``'s own docstring for the
enumerated list). ``Session.mcp_subscription_state()`` compares the LIVE
generation to the one its cached value was computed against on every
read; no subscriber registration exists for this cache at all any more —
the connection service does not need to know the cache exists, and a
NEW mutation site added inside ``MCPConnectionService`` automatically
covers this cache correctly as long as it calls ``_bump_generation()``
(colocated with the real mutation, not a hand-picked event-kind guess
one layer removed from it).

Real ``Session`` + real ``MCPConnectionService`` (no held servers — this
file tests the CACHING mechanism, not connection/subscription composition
correctness, which #4686's own suite already covers against a real MCP
subprocess). Uses the #4403 counting technique (wrap the real
``subscription_summary``, count real calls). Generation bumps are driven
directly via the connection service's own real internal mutation methods
(``_track_subscription``/``_untrack_subscription``/``aclose``) — the SAME
methods #5287's implementation instrumented, not a synthetic stand-in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "alice" / "state" / "snapshot.json",
    )


def _counting_wrapper(monkeypatch, service) -> dict:
    """Mirrors #4403's own counting technique, applied to this session's
    OWN ``MCPConnectionService`` instance (bound method, not the class) —
    counts real ``subscription_summary()`` calls from this point on."""
    real_fn = service.subscription_summary
    call_count = {"n": 0}

    def _counting():
        call_count["n"] += 1
        return real_fn()

    monkeypatch.setattr(service, "subscription_summary", _counting)
    return call_count


@pytest.mark.asyncio
async def test_repeated_reads_cost_one_real_compute(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — 3 repeated ``mcp_subscription_state()`` reads
    with NO intervening generation bump cost exactly 1 real
    ``subscription_summary`` call (the first, lazy fill), not 3."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    r1 = s.mcp_subscription_state()
    r2 = s.mcp_subscription_state()
    r3 = s.mcp_subscription_state()

    assert call_count["n"] == 1, (
        f"expected exactly 1 real subscription_summary call across 3 reads "
        f"with no intervening generation bump, got {call_count['n']}"
    )
    assert r1 == r2 == r3 == []


@pytest.mark.asyncio
async def test_a_real_mutation_is_seen_on_the_next_read_only(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — after the first (lazy) read, a real
    ``MCPConnectionService`` mutation (``_track_subscription``, the same
    private method ``_HeldConnection.subscribe_resource`` calls on a
    successful subscribe) bumps ``generation``; the cache does not
    recompute until the NEXT read, and a read with no further mutation in
    between costs nothing more."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    # Drives the real mutation directly (the same private method
    # ``_HeldConnection.subscribe_resource`` calls on a successful
    # subscribe) — a generation VALUE is an implementation detail this
    # test does not assert on directly (Tier 4: no private-state assert);
    # what's observable, and asserted below, is the CACHE's own behavior.
    s._mcp_connection_service._track_subscription("srv", "resource://x")
    assert call_count["n"] == 1, (
        f"bumping generation must NOT itself trigger a real call, got "
        f"{call_count['n']} real calls total"
    )

    # The next real read pays for exactly one more real call (the lazy fill).
    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"expected exactly 1 more real call on the first read after the "
        f"generation bump, got {call_count['n']} real calls total"
    )

    # A further read with no intervening mutation is once again a bare
    # cache return — 0 additional calls.
    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"a read with no intervening mutation must cost nothing more, got "
        f"{call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_aclose_also_bumps_generation(tmp_path, monkeypatch) -> None:
    """Tier 2: #5287 — ``aclose()`` (teardown) is one of ``_bump_generation``'s
    own enumerated sites: it clears ``_clients``/``_subscription_adapters``,
    both real inputs to ``subscription_summary()``. Same shape as the
    previous test, for this mutation site."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    await s._mcp_connection_service.aclose()

    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"expected exactly 1 more real call on the first read after aclose(), "
        f"got {call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_an_event_emit_alone_no_longer_triggers_a_recompute(tmp_path, monkeypatch) -> None:
    """Tier 2: #5287 — deliberate simplification, stated as a positive
    assertion (not merely "still passes"): this cache no longer subscribes
    to ANY EventLog event kind at all (the pre-#5287 hand-picked 7-kind
    list — #5276/#5279/#5280 — needed a kind added after shipping once
    already). Emitting a real subscription-shaped event with NO
    accompanying ``MCPConnectionService`` mutation must not recompute —
    only a real generation bump does."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    s._audit_events.emit("mcp_resource_subscribed", server="srv", uri="resource://x")
    await settle(s._audit_events)

    s.mcp_subscription_state()
    assert call_count["n"] == 1, (
        f"an event emit with no real MCPConnectionService mutation behind it "
        f"must not trigger a recompute, got {call_count['n']} real calls total"
    )
