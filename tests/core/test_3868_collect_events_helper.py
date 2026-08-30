"""Tier 2: #3868 PR-2 — the ``collect_events`` test helper.

Real ``EventLog`` throughout (no mocks) — the helper is a thin wrapper over
``EventLog.add_subscriber``, so faking it would test nothing real.

#5467 phase 1 (architect ruling): a real ``Session`` also throughout for the
new ``Session``-acceptance tests below — the design's own bar is that this
seam works with the exact object a caller who only has a ``Session`` (never
its internal ``EventLog``) actually holds.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from tests._support.events import _resolve_log, collect_events, settle
from tests._support.session import make_session


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


# ---------------------------------------------------------------------------
# #5467 phase 1 — Session acceptance (architect ruling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_events_accepts_a_session_witness_1(tmp_path, monkeypatch) -> None:
    """Tier 2: #5467 witness ① — a caller holding only a ``Session`` (never
    its internal ``EventLog``) can call ``collect_events(session)`` directly
    and genuinely capture a real emit — asserted on the PUBLIC-shaped
    behavior (what was collected), never by introspecting ``_audit_events``'
    own subscriber list (this repo's private-state rule: the private reach
    inside ``_resolve_log`` drives the scenario here, exactly like every
    other test in this file already drives its own EventLog by calling
    ``.emit()`` directly; nothing here ASSERTS on private state)."""
    session = make_session(tmp_path, monkeypatch=monkeypatch)
    collected = collect_events(session)
    session._audit_events.emit("tool_executed", op="read_file", path="/tmp/x")
    # #5467: deliberately ``session._audit_events.drain()``, NOT
    # ``settle(session)`` — this witness's own claim is "collect_events(session)
    # alone genuinely resolves and subscribes", so draining through the OTHER
    # helper here would let a broken settle() silently backstop a broken
    # collect_events() (or vice versa) and this test would stay green either
    # way. Witness② (test_settle_accepts_a_session_witness_2, below) is where
    # settle(session) itself gets exercised — the two stay independent on
    # purpose. This line is intentionally excluded from #5467's migration.
    await session._audit_events.drain()
    assert [e.type for e in collected] == ["tool_executed"]


@pytest.mark.asyncio
async def test_settle_accepts_a_session_witness_2(tmp_path, monkeypatch) -> None:
    """Tier 2: #5467 witness ② — ``await settle(session)`` drains the
    SESSION'S OWN real ``_audit_events`` queue (never a private reach at the
    call site — the private reach happens only inside ``_resolve_log``, the
    one place #5467's design permits it). A real emit through the session's
    own log, collected via the same seam, proves the drain reaches the
    right queue."""
    session = make_session(tmp_path, monkeypatch=monkeypatch)
    collected = collect_events(session)
    session._audit_events.emit("tool_executed", op="read_file", path="/tmp/x")
    await settle(session)
    assert [e.type for e in collected] == ["tool_executed"]


def test_resolve_log_passes_a_plain_eventlog_through_unchanged() -> None:
    """Tier 2: an object with no ``_audit_events`` attribute (a real
    ``EventLog``, or any other log-shaped test double) passes through
    ``_resolve_log`` unchanged — the ``Session`` branch is additive, never a
    behavior change for every existing non-``Session`` caller."""
    log = EventLog()
    assert _resolve_log(log) is log


def test_session_itself_carries_the_underlying_audit_events_attribute(tmp_path, monkeypatch) -> None:
    """Tier 2: accept-side half of the load-bearing witness — a real
    ``Session`` genuinely carries ``_audit_events`` (what ``_resolve_log``
    actually resolves to), so the two Session-acceptance tests above are
    exercising real resolution, not a coincidence of a broken
    ``make_session`` vacuously satisfying them (architect review, #5484).

    #5507 correction (lead-coder BLOCKING, same family #5449/#5500
    already closed — see ``test_5447_doctor_hook_env_single_source.py:
    24-30`` and ``test_5494_hooks_helper.py``'s own module docstring for
    the two prior instances): this test used to ALSO assert ``not
    hasattr(session, "add_subscriber")`` / ``"drain"`` as the deny-side
    half of the "load-bearing" witness. That shape is inverted (the day
    ``Session`` legitimately grows an ``add_subscriber``/``drain``-named
    method for a real reason, this test turns red and punishes the
    fixer, not a regression) and incomplete (a differently-named
    equivalent leaves it green while the claim goes false). Deleted.

    Load-bearing-ness of ``_resolve_log``'s ``Session``-detection branch
    is shown by STRIP-FALSIFY instead, performed BY HAND (#5507):
    temporarily forcing ``_resolve_log`` to return *obj* unchanged makes
    both Session-acceptance tests above fail with ``AttributeError:
    'Session' object has no attribute 'add_subscriber'`` — confirmed,
    reverted."""
    session = make_session(tmp_path, monkeypatch=monkeypatch)
    assert hasattr(session, "_audit_events")
