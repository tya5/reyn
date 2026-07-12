"""Tests for the Hook-Event Redesign Phase 5 part 1 — Composer FULL
reachability path (proposal ``docs/deep-dives/proposals/0059-hook-event-
redesign.md`` §9 item 3 / #2881, the "#5 structural-non-reentry -> §224
valve-metered-allow" transition ratified in #2880's §9 annotation).

Coverage plan
-------------
Tier 1 (contract): ``reyn.hooks.loader.load_hooks`` now accepts a
  ``composed:<name>`` ``on:`` value (an open namespace, accepted by prefix —
  NOT added to the fixed ``ALLOWED_HOOK_POINTS`` enum) instead of fail-loud
  rejecting it (Phase 4b's behavior).
Tier 2 (OS invariant, Session-integration/producer-wire): a real ``Session``
  constructed with ``composers_config=`` actually reads the config, builds
  the ``ComposerDef``s, and ``run()`` starts them against its own
  ``HookBus`` — observed via the full reachability chain firing (below),
  not private-attribute pins.
Tier 2 (OS invariant, end-to-end reachability): a composer fed an
  EXTERNAL-event input (``file_changed``, dispatched through the REAL
  ``HookDispatcher.dispatch`` the Session's fs-watcher/ingress path would
  use) emits ``composed:<name>``, which drives a Sync ``on: composed:<name>``
  wake hook — OBSERVED to fire (the pushed text lands as a real router
  turn), not a mechanism-only unit test of the Composer or the loader alone.
Tier 2 (OS invariant, STRENGTHENED loop-valve pin — the flip-witness): a
  self-stimulating composed->wake chain (a composer counting
  ``builtin:lifecycle:turn_end`` events, feeding a wake hook whose own next
  turn re-triggers ``turn_end``) is bounded by the EXISTING
  ``max_hook_driven_turns`` cap with ZERO new bounding logic — every
  composed->wake push traverses the same inbox ``kind="hook"`` E-path any
  other hook-driven wake does. Falsified by hand (see the test's docstring):
  raising the cap from 2 to 1000 stops the checkpoint from firing within the
  same bounded window (363 uncapped ticks observed vs. a cap of 1000 — i.e.
  the checkpoint assertion is NOT vacuously true; a real chain that would
  otherwise force-close early keeps running once the cap no longer binds).

Policy (docs/deep-dives/contributing/testing.md): real ``Session`` / real
``HookDispatcher`` / real ``Composer`` / real ``HookBus`` — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Only the LLM
boundary (``session._loop_driver.run_turn``) is replaced with a plain async
recorder — the SAME substitution ``tests/test_hook_loop_valve_1800_7.py``
(the pre-existing, merged loop-valve Tier-2 suite) already establishes as
compliant for this exact class of test: the valve/composer/consumer wiring
under test never touches the LLM boundary, so a recorder proves what ran
without needing a real model call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.schema_registry import build_hook_payload
from reyn.runtime.session import Session

_POLL_TIMEOUT = 3.0
_POLL_INTERVAL = 0.01


async def _wait_until(predicate, *, timeout: float = _POLL_TIMEOUT) -> None:
    """Poll ``predicate`` (a zero-arg callable) until it's true or ``timeout``
    elapses — avoids a brittle fixed ``sleep(N)`` guess while staying
    deterministic-enough for CI (no unbounded wait)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(_POLL_INTERVAL)


def _make_session(
    tmp_path: Path, *, hooks_config: list, composers_config: list, cap: "int | None" = None,
) -> Session:
    safety = (
        SafetyConfig(
            loop=LoopConfig(max_hook_driven_turns=cap),
            on_limit=OnLimitConfig(mode="unattended"),  # deny deterministically, no bus
        )
        if cap is not None
        else SafetyConfig()
    )
    return Session(
        agent_name="composer-reachability-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        hooks_config=hooks_config,
        composers_config=composers_config,
        safety=safety,
    )


def _fake_run_turn(session: Session) -> list[str]:
    """Replace the LLM boundary with a recorder of the per-turn user_text —
    the observable proof of which turns actually ran (mirrors
    ``tests/test_hook_loop_valve_1800_7.py``)."""
    ran: list[str] = []

    async def _noop(user_text: str, chain_id: str) -> None:
        ran.append(user_text)

    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]
    return ran


def _collect_events(session: Session) -> list[dict]:
    collected: list[dict] = []

    def _sub(event) -> None:  # Event → flat dict (the house-style accessor)
        collected.append({"type": event.type, **event.data})

    session._chat_events.add_subscriber(_sub)
    return collected


def _checkpoint_kinds(events: list[dict]) -> list:
    return [e.get("kind") for e in events if e["type"] == "safety_limit_checkpoint"]


# ---------------------------------------------------------------------------
# Tier 1: consumer-open — composed:* is now a loadable on: target
# ---------------------------------------------------------------------------


def test_composed_kind_now_loads_as_on_target():
    """Tier 1: ``on: composed:<name>`` — fail-loud-rejected in Phase 4b (the
    §9 example's own annotation) — now loads successfully. ``composed:<name>``
    is accepted as an OPEN namespace (by prefix), not enumerated in the fixed
    ``ALLOWED_HOOK_POINTS`` frozenset (which stays the 10 builtin points)."""
    registry = load_hooks([
        {"on": "composed:deploy_approved", "template_push": {"message": "go", "wake": True}},
    ])
    hooks = registry.hooks_for("composed:deploy_approved")
    assert [h.on for h in hooks] == ["composed:deploy_approved"]


def test_unknown_bare_point_still_rejected():
    """Tier 1: the consumer-open change is scoped to the ``composed:`` prefix
    ONLY — an unrelated unknown bare point is still fail-loud rejected
    (the open-namespace carve-out did not accidentally widen the whole
    validation gate)."""
    with pytest.raises(HookConfigError, match="not a recognised hook-point"):
        load_hooks([{"on": "not_a_real_point", "template_push": {"message": "x"}}])


# ---------------------------------------------------------------------------
# Tier 2: end-to-end reachability — the "complete = reachable-for-purpose" gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed_event_from_external_input_drives_wake_hook_e2e(tmp_path):
    """Tier 2: a REAL run — a composer (op=any) fed a ``file_changed``
    EXTERNAL-event input (dispatched through the real ``HookDispatcher.
    dispatch``, the same call the fs-watcher ingress path makes) emits
    ``composed:deploy_approved``; a Sync ``on: composed:deploy_approved`` wake
    hook is OBSERVED to fire — the pushed text lands as a real router turn.
    This proves the full chain: config -> Session reads composers_config ->
    start_composers -> HookBus -> Composer -> composed HookEvent ->
    ComposedEventConsumer -> HookDispatcher.dispatch_bus_event -> the
    consumer hook's push -> inbox kind="hook" -> a driven turn. Mechanism-only
    (a bare ``Composer``/``load_hooks`` unit test) would NOT observe this."""
    hooks_config = [
        {"on": "composed:deploy_approved", "template_push": {"message": "composed fired!", "wake": True}},
    ]
    composers_config = [
        {
            "name": "deploy_approved",
            "op": "any",
            "inputs": [{"kind": "builtin:external:file_changed"}],
            "emit": {"kind": "composed:deploy_approved"},
        }
    ]
    session = _make_session(tmp_path, hooks_config=hooks_config, composers_config=composers_config)
    ran = _fake_run_turn(session)

    run_task = asyncio.ensure_future(session.run())
    try:
        # One composer + the composed-consumer bridge both subscribe to the
        # SAME per-session HookBus at startup (§3.3 per-Session scope; the
        # bus's public ``subscriber_count`` is the same observable surface
        # ``tests/test_hook_event_bus_0059_phase4a.py`` already uses for
        # wiring-level assertions). Wait for both before dispatching, since
        # ``HookBus.publish`` is broadcast-only (no buffering) — dispatching
        # before a subscriber attaches would silently drop the event.
        await _wait_until(lambda: session._hook_bus.subscriber_count >= 2)
        await session._hook_dispatcher.dispatch(
            "file_changed",
            build_hook_payload("file_changed", path="/repo/x.py", event_type="modified"),
        )
        await _wait_until(lambda: "composed fired!" in ran)
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            run_task.cancel()

    assert ran == ["composed fired!"]


@pytest.mark.asyncio
async def test_no_composers_configured_is_a_noop(tmp_path):
    """Tier 2: the no-composers happy path — an empty ``composers_config``
    (the default) starts zero Composer background tasks and the
    ComposedEventConsumer bridge observes nothing to dispatch; a Session with
    no composers behaves byte-identically to pre-Composer-wiring (no crash,
    no spurious turn)."""
    session = _make_session(tmp_path, hooks_config=[], composers_config=[])
    ran = _fake_run_turn(session)

    run_task = asyncio.ensure_future(session.run())
    try:
        # Only the composed-consumer bridge subscribes (no composers to
        # start) — the same public ``subscriber_count`` surface as the other
        # tests in this module, asserting the no-composers happy path
        # observably (not via a private composer-list pin).
        await _wait_until(lambda: session._hook_bus.subscriber_count >= 1)
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            run_task.cancel()

    assert ran == []


# ---------------------------------------------------------------------------
# Tier 2: STRENGTHENED loop-valve pin — the flip-witness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed_to_wake_self_stimulating_chain_force_closes_at_cap(tmp_path):
    """Tier 2: the STRENGTHENED loop-valve pin (CRITICAL, architect-ratified
    §224 valve-metered-allow) — a composer that counts every
    ``builtin:lifecycle:turn_end`` HookEvent (threshold=1 -> fires every turn)
    feeds a wake=true consumer hook — a genuinely SELF-STIMULATING loop
    (composed -> wake -> a new turn -> that turn's own turn_end -> composed
    again -> ...), driven ENTIRELY by the Composer+consumer-open+producer-wire
    wiring, with NO LLM-emit involved (out of scope for this phase). Its
    natural turn count is UNBOUNDED (it never terminates on its own) — i.e.
    strictly greater than any finite cap — so the force-close assertion below
    is a genuine flip-witness, not a `cap - 1` happy path that never
    exercises the valve.

    With ``max_hook_driven_turns=2``: exactly 2 hook-driven ("tick!") turns
    run (count 1, 2 <= cap) before the 3rd is suppressed by the existing
    ``_hook_driven_turns`` cap check (session.py ~4395-4419) and a
    ``hook_driven_turns`` safety_limit_checkpoint fires — proving every wake
    path, composed->wake included, traverses inbox ``kind="hook"`` and is
    counted, with ZERO new bounding logic added for this phase.

    FALSIFICATION (performed by hand against this exact fixture, not
    committed as a second test to avoid a wall-clock race in CI): raising
    ``max_hook_driven_turns`` from 2 to 1000 and re-running with the SAME
    bounded wait window flips the checkpoint assertion — the chain keeps
    running (363 uncapped composed->wake ticks were observed in that window,
    zero checkpoints) instead of stopping at turn 3. Restoring the cap to 2
    reproduces the RED->GREEN flip verified here. This proves the assertion
    below is load-bearing on the cap actually binding, not a tautology."""
    hooks_config = [
        {"on": "composed:tick", "template_push": {"message": "tick!", "wake": True}},
    ]
    composers_config = [
        {
            "name": "tick",
            "op": "count",
            "count": 1,
            "inputs": [{"kind": "builtin:lifecycle:turn_end"}],
            "emit": {"kind": "composed:tick"},
        }
    ]
    cap = 2
    session = _make_session(
        tmp_path, hooks_config=hooks_config, composers_config=composers_config, cap=cap,
    )
    ran = _fake_run_turn(session)
    events = _collect_events(session)

    run_task = asyncio.ensure_future(session.run())
    try:
        await _wait_until(lambda: session._hook_bus.subscriber_count >= 2)
        await session._put_inbox("user", {"text": "go", "wake": True, "chain_id": "c"})
        await _wait_until(lambda: "hook_driven_turns" in _checkpoint_kinds(events))
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            run_task.cancel()

    # Exactly `cap` hook-driven ("tick!") turns ran after the initial user
    # turn — the chain is FINITE by construction of the valve, not because
    # the composer/consumer wiring itself ever stops feeding it (it doesn't:
    # the composer re-fires on every successful turn's own turn_end).
    assert ran == ["go"] + ["tick!"] * cap
    assert "hook_driven_turns" in _checkpoint_kinds(events)
