"""Tier 2: #5276 — ``Session.mcp_subscription_state()``'s reactive cache.

Root cause: this method used to forward straight to
``MCPConnectionService.subscription_summary()`` on EVERY call, including
every render frame the status panel drew. That composition is real work
(iterates every held server, per-URI honored-set lookups) for a value that
only ever changes on a handful of specific events (subscribe/unsubscribe,
a server install/remove, or a (re)connect). Fix: a subscriber registered
once in ``Session.__init__`` recomputes EAGERLY (off the render path) only
when one of those events fires; every other call is a bare cached-attribute
return.

Real ``Session`` + real ``MCPConnectionService`` (no held servers — this
file tests the CACHING mechanism, not connection/subscription composition
correctness, which #4686's own suite already covers against a real MCP
subprocess). Uses the #4403 counting technique (wrap the real
``subscription_summary``, count real calls) and
``tests/_support/events.py``'s ``settle`` (#3868/#4966) to wait for the
subscriber's queued dispatch before reading the post-event state.
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
async def test_a_relevant_event_triggers_exactly_one_recompute(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — after the first (lazy) read, emitting ONE of
    the events that can change the subscription summary triggers exactly
    ONE recompute (in the subscriber callback, off the render path) —
    demonstrated by a SUBSEQUENT read costing 0 additional real calls."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s._mcp_connection_service)

    s.mcp_subscription_state()  # lazy fill: 1 real call
    assert call_count["n"] == 1

    s._audit_events.emit("mcp_resource_subscribed", server="srv", uri="resource://x")
    await settle(s._audit_events)

    assert call_count["n"] == 2, (
        f"expected the subscriber to have recomputed once by now (off the "
        f"render path), got {call_count['n']} real calls total"
    )

    # A subsequent read is now a bare cache return — 0 additional calls.
    s.mcp_subscription_state()
    assert call_count["n"] == 2, (
        f"a read AFTER the subscriber already recomputed must cost nothing "
        f"more, got {call_count['n']} real calls total"
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

    s._audit_events.emit("user_submitted", text="hello", chain_id="c1", msg_id="m1", seq=1)
    await settle(s._audit_events)

    assert call_count["n"] == 1, (
        f"an unrelated event kind must not trigger a recompute, got "
        f"{call_count['n']} real calls total"
    )
