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
(Pre-#5561 a "STRENGTHENED loop-valve pin — the flip-witness" test lived
  here: a self-stimulating composed->wake chain — a composer counting
  ``builtin:lifecycle:turn_end`` events, feeding a wake hook whose own next
  turn re-triggers ``turn_end`` — bounded by the ``max_hook_driven_turns``
  cap, hand-falsified by raising the cap from 2 to 1000 and observing 363
  uncapped ticks with zero checkpoints. #5561 (owner ruling) retired that
  loop valve entirely; the test and its dedicated helpers were deleted with
  it — the self-stimulating CHAIN this test constructed is unaffected by
  the retirement and would still run today, it simply no longer force-
  closes at any built-in cap; see ``LoopConfig``'s own docstring,
  config/chat.py, for the replacement bounding mechanisms.)

Policy (docs/deep-dives/contributing/testing.md): real ``Session`` / real
``HookDispatcher`` / real ``Composer`` / real ``HookBus`` — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Only the LLM
boundary (``session._loop_driver.run_turn``) is replaced with a plain async
recorder — the SAME substitution ``tests/core/test_hook_loop_valve_1800_7.py``
already establishes as compliant for this exact class of test: the
composer/consumer wiring under test never touches the LLM boundary, so a
recorder proves what ran without needing a real model call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config.chat import SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.schema_registry import build_hook_payload
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_POLL_TIMEOUT = 3.0
_POLL_INTERVAL = 0.01


async def _wait_until(predicate, *, delay: float = _POLL_INTERVAL) -> None:
    """Poll ``predicate`` (a zero-arg callable) until it's true — waiting for
    composer/consumer wiring (bus subscriber counts, a pushed turn landing, a
    safety_limit_checkpoint firing) to become observable. Unbounded per the
    owner's testing policy (docs/deep-dives/contributing/testing.md, ## Time):
    no test carries a time budget, marker or in-body -- a slower environment
    only makes this slower, never fail it; CI's --timeout=120 is the
    blast-radius kill-switch, not a contract."""
    while not predicate():
        await asyncio.sleep(delay)


def _make_session(
    tmp_path: Path, *, hooks_config: list, composers_config: list,
) -> Session:
    # (#5561 removed this helper's `cap` param — it built a
    # `SafetyConfig(loop=LoopConfig(max_hook_driven_turns=cap))` branch used
    # only by the now-deleted flip-witness test.)
    safety = SafetyConfig()
    return make_session(
        agent_name="composer-reachability-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config, composers_config=composers_config),
        safety=safety,
    )


def _fake_run_turn(session: Session) -> list[str]:
    """Replace the LLM boundary with a recorder of the per-turn user_text —
    the observable proof of which turns actually ran (mirrors
    ``tests/core/test_hook_loop_valve_1800_7.py``)."""
    ran: list[str] = []

    async def _noop(user_text: str, chain_id: str) -> None:
        ran.append(user_text)

    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]
    return ran


# (#5561 removed `_collect_events`/`_checkpoint_kinds` — both existed only
# to observe the now-deleted flip-witness test's safety_limit_checkpoint.)


# ---------------------------------------------------------------------------
# Tier 1: consumer-open — composed:* is now a loadable on: target
# ---------------------------------------------------------------------------


def test_composed_kind_now_loads_as_on_target():
    """Tier 1: ``on: composed:<name>`` — fail-loud-rejected in Phase 4b (the
    §9 example's own annotation) — now loads successfully. ``composed:<name>``
    is accepted as an OPEN namespace (by prefix), not enumerated in the fixed
    ``ALLOWED_HOOK_POINTS`` frozenset (the fixed set of builtin points)."""
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
        # ``tests/hooks/test_hook_event_bus_0059_phase4a.py`` already uses for
        # wiring-level assertions). Wait for both before dispatching, since
        # ``HookBus.publish`` is broadcast-only (no buffering) — dispatching
        # before a subscriber attaches would silently drop the event.
        await _wait_until(lambda: session._hook_bus.subscriber_count >= 2)
        await session._hook_dispatcher.dispatch(
            "file_changed",
            build_hook_payload("file_changed", path="/repo/x.py", event_type="modified"),
        )
        # #5686/#5687: the pushed text now lands ATTRIBUTED
        # ("[hook:<name>] composed fired!"), not bare — the correct
        # tightening of this test's own claim ("the pushed text lands as
        # a real router turn"), not a weakening: _handle_hook_message's
        # turn seed matches its history entry / outbox announcement now,
        # instead of drifting from them (a #3595-class misattribution
        # this PR closed). A membership/equality check against the bare
        # string would wait forever for a value that never arrives again
        # — partial-match (`in`) on the composed text is what survives a
        # future attribution-prefix change without re-encoding the exact
        # prefix shape here.
        await _wait_until(lambda: any("composed fired!" in r for r in ran))
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            run_task.cancel()

    (only_turn,) = ran  # exactly one hook-driven turn — a 2nd or 0 fails to unpack
    assert "composed fired!" in only_turn, (
        f"expected the one hook-driven turn's text to contain "
        f"'composed fired!' (attributed, per #5686) — got {only_turn!r}"
    )
    assert only_turn.startswith("[hook:"), (
        f"the hook-driven turn's own seed must carry the [hook:<name>] "
        f"attribution prefix (#5686) — got {only_turn!r}"
    )


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


# (#5561 retired the STRENGTHENED loop-valve pin — the flip-witness — that
# lived here: test_composed_to_wake_self_stimulating_chain_force_closes_at_
# cap, hand-falsified by raising the cap 2->1000 and observing 363 uncapped
# ticks with zero checkpoints. The mechanism it pinned is gone; deleted
# with it. See git history for the fixture if this self-stimulating-chain
# concern needs a replacement bound in the future.)
