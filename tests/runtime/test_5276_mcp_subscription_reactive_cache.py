"""Tier 2: #5276 — ``Session.mcp_subscription_state()``'s reactive cache.

Root cause: this method used to forward straight to
``MCPConnectionService.subscription_summary()`` on EVERY call, including
every render frame the status panel drew. That composition is real work
(iterates every held server, per-URI honored-set lookups) for a value that
only ever changes on a handful of specific events (subscribe/unsubscribe,
a server install/remove, or a (re)connect). Fix: a subscriber registered
once in ``Session.__init__`` marks the cache DIRTY (``None``) — never
recomputes itself — when one of those events fires; the actual
recomputation happens lazily, on the next real ``mcp_subscription_state()``
call, on that caller's own stack.

Corrected mid-review (architect BLOCK on an earlier draft that recomputed
EAGERLY inside the subscriber callback, #5279): (1) ``EventLog``'s
per-subscriber try/except (#4963/#4961 A) would silently swallow a raise
from an eager recompute, leaving a stale value in place with nobody aware
a refresh failed; (2) recomputing per EVENT ties the real cost to
whatever paces those events (e.g. a remote MCP server's own reconnect
cadence), not to whether anyone is actually reading the value — a
headless run that never reads this went from 0 real computations to one
per event. Marking dirty instead bounds the cost to actual reads, exactly
like every other cache in this file.

Real ``Session`` + real ``MCPConnectionService`` (no held servers — this
file tests the CACHING mechanism, not connection/subscription composition
correctness, which #4686's own suite already covers against a real MCP
subprocess). Uses the #4403 counting technique (wrap the real
``subscription_summary``, count real calls) and
``tests/_support/events.py``'s ``settle`` (#3868/#4966) to wait for the
subscriber's queued dispatch before reading the post-event state.

#5280 (found while answering architect's #5279 review question, fixed in
that issue's own PR): a 7th kind, ``mcp_reconnect_failed``, was added to
the subscriber list — a FAILED reconnect (``MCPConnectionService.
_reconnect``) drops a server from ``held_servers()`` without ever
reaching the success-only ``mcp_initialized`` emit, so none of the
original 6 kinds fired on that path. See
``test_mcp_reconnect_failed_also_marks_dirty`` below for the cache-side
witness, and ``tests/mcp/test_5280_mcp_reconnect_failed_event.py`` for
the real ``MCPConnectionService._reconnect`` witness that the event
actually fires on a genuine reopen failure.
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
    with NO intervening event cost exactly 1 real ``subscription_summary``
    call (the first, lazy fill), not 3."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    r1 = s.mcp_subscription_state()
    r2 = s.mcp_subscription_state()
    r3 = s.mcp_subscription_state()

    assert call_count["n"] == 1, (
        f"expected exactly 1 real subscription_summary call across 3 reads "
        f"with no intervening event, got {call_count['n']}"
    )
    assert r1 == r2 == r3 == []


@pytest.mark.asyncio
async def test_a_relevant_event_marks_dirty_but_does_not_itself_recompute(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — after the first (lazy) read, emitting ONE of
    the events that can change the subscription summary marks the cache
    dirty WITHOUT itself costing a real call (the subscriber only sets
    ``None`` — #5279 review: recomputing inside the subscriber would
    silently swallow a raise via EventLog's per-subscriber try/except, and
    would tie the real cost to event cadence rather than actual reads).
    The NEXT real read is what pays for exactly one more real call."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    # #5557: this emit only drives the dirty-marking scenario — every
    # assert in this test reads `call_count` (an unrelated collaborator's
    # own call tally via `_counting_wrapper`), never this emit's own
    # type/data. The event kind matters only insofar as it's IN the
    # subscribed set; the payload is arbitrary.
    s._audit_events.emit("mcp_resource_subscribed", server="srv", uri="resource://x")
    await settle(s._audit_events)

    assert call_count["n"] == 1, (
        f"the event itself must NOT trigger a real call (only marks dirty), "
        f"got {call_count['n']} real calls total"
    )

    # The next real read pays for exactly one more real call (the lazy fill).
    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"expected exactly 1 more real call on the first read after the "
        f"dirty-marking event, got {call_count['n']} real calls total"
    )

    # A further read with no intervening event is once again a bare cache
    # return — 0 additional calls.
    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"a read with no intervening event must cost nothing more, got "
        f"{call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_mcp_reconnect_failed_also_marks_dirty(tmp_path, monkeypatch) -> None:
    """Tier 2: #5280 — a 7th subscribed kind, added after this file's own
    original 6 (found while answering architect's #5279 review question):
    a FAILED reconnect (``MCPConnectionService._reconnect``) drops a server
    from ``held_servers()`` without ever reaching the success-only
    ``mcp_initialized`` emit — this kind is what invalidates the cache on
    that path instead. Same shape as
    ``test_a_relevant_event_marks_dirty_but_does_not_itself_recompute``
    above, for the new kind."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    # #5557: same reasoning as the previous test — drives dirty-marking,
    # every assert reads `call_count`, not this emit's own event.
    s._audit_events.emit("mcp_reconnect_failed", server="srv")
    await settle(s._audit_events)

    assert call_count["n"] == 1, (
        f"the event itself must NOT trigger a real call (only marks dirty), "
        f"got {call_count['n']} real calls total"
    )

    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"#5280 REGRESSION: expected exactly 1 more real call on the first "
        f"read after mcp_reconnect_failed, got {call_count['n']} real calls total"
    )


@pytest.mark.asyncio
async def test_an_unrelated_event_does_not_trigger_a_recompute(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification contrast — an event OUTSIDE the subscribed
    kind set must not recompute (proves the subscriber is kind-filtered,
    not firing on every event)."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    # #5557: same reasoning — this is the falsification contrast (an
    # unrelated kind), asserts still only read `call_count`.
    s._audit_events.emit("user_submitted", text="hello", chain_id="c1", msg_id="m1", seq=1)
    await settle(s._audit_events)

    assert call_count["n"] == 1, (
        f"an unrelated event kind must not trigger a recompute, got "
        f"{call_count['n']} real calls total"
    )
