"""Tier 2: #3868 PR-2 — the ``collect_events`` test helper.

Real ``EventLog`` throughout (no mocks) — the helper is a thin wrapper over
``EventLog.add_subscriber``, so faking it would test nothing real.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from tests._support.events import collect_events


@pytest.mark.asyncio
async def test_collect_events_captures_emits_after_the_call() -> None:
    """Tier 2: an event emitted AFTER collect_events() is called appears in
    the returned list.

    #4961 C: dispatch moved off of `emit()`'s own synchronous caller onto
    a queue-consumer task — yields once (`await asyncio.sleep(0)`) after
    emitting so the consumer actually runs before asserting delivery."""
    log = EventLog()
    collected = collect_events(log)
    log.emit("tool_executed", op="read_file", path="/tmp/x")
    await log.drain()
    assert [e.type for e in collected] == ["tool_executed"]


def test_collect_events_does_not_retroactively_capture_prior_emits() -> None:
    """Tier 2: the one real behavior difference from ``.all()`` — a
    subscriber only sees what is emitted AFTER it is added, so an emit
    BEFORE collect_events() is called is not retroactively captured. This
    is why the call must move to right after construction, not stay at the
    assertion site (see module docstring's own note on this)."""
    log = EventLog()
    log.emit("llm_request", model="x")
    collected = collect_events(log)
    assert collected == []


@pytest.mark.asyncio
async def test_collect_events_list_is_live_across_repeated_reference() -> None:
    """Tier 2: the returned list keeps growing as the log emits — a polling
    pattern (``any(... for e in collected)`` called repeatedly, matching the
    real-world wait-until-condition shape many existing tests use) sees each
    new emit without calling collect_events() again.

    #4961 C: yields once after emit — see the file's first test for why."""
    log = EventLog()
    collected = collect_events(log)
    assert not any(e.type == "config_reloaded" for e in collected)
    log.emit("config_reloaded", source="test")
    await log.drain()
    assert any(e.type == "config_reloaded" for e in collected)


@pytest.mark.asyncio
async def test_collect_events_captures_multiple_emits_in_order() -> None:
    """Tier 2: multiple emits are collected in emission order — matching
    ``.all()``'s own ordering guarantee, so a mechanical replacement
    preserves any order-dependent assertion.

    #4961 C: yields once after emit — see the file's first test for why."""
    log = EventLog()
    collected = collect_events(log)
    log.emit("a")
    log.emit("b")
    log.emit("c")
    await log.drain()
    assert [e.type for e in collected] == ["a", "b", "c"]


def test_collect_events_uses_the_real_subscriber_mechanism_not_a_readback() -> None:
    """Tier 2: strip-falsify — removing the ``add_subscriber`` call inside
    ``collect_events`` (simulated here by using a real EventLog whose
    subscriber list is never populated) makes this fail the same way it
    would if ``collect_events`` degraded to a no-op stub. Verifies the
    helper is genuinely wired to production's real subscriber path
    (``EventLog.add_subscriber``/``emit``'s ``for sub in self._subscribers``
    loop), not a separately-maintained read-back of ``_events``."""
    log = EventLog()
    collected = collect_events(log)
    assert log.subscribers == [collected.append]
