"""Tier 2: ChannelState webhook wire (issue #269 Phase 2).

Pre-#269 Phase 2 (= up to PR #274): ``ChannelState`` /
``DeliveryResult`` / ``RetryPolicy`` vocabulary existed but no
production code path instantiated ``ChannelState``. Outbound webhooks
fired indefinitely even when the peer's URL kept returning 5xx /
timing out → wasted retries + log noise.

This file pins Phase 2 wire: per-``RunEntry`` ``ChannelState``
tracks consecutive webhook failures and short-circuits subsequent
fires once the threshold is crossed.

Two production call sites updated by this PR:

  - ``A2AInterventionBus.on_dispatch`` — fires the input-required
    payload to the peer's webhook on each iv dispatch.
  - ``_A2AProgressBridge._send`` — fires the in-progress payloads
    for each tracked chat event (phase / llm / act).

Both share the same ``RunRegistry.webhook_channel_state(run_id)``
ChannelState instance so an alternating sequence (input-required →
progress → completed) participates in one dead-channel inference.

Pins:

  1. ``RunRegistry.webhook_channel_state`` returns None when the
     RunEntry has no ``webhook_url`` (= peer didn't opt in to push).
  2. Returns a lazy-initialised ``ChannelState`` keyed by
     ``f"webhook:{run_id}"`` when ``webhook_url`` is set; subsequent
     calls return the same instance.
  3. ``A2AInterventionBus.on_dispatch`` calls ``record_attempt``
     after each ``post_webhook`` invocation, and skips the post
     entirely when ``is_alive()`` returns False.
  4. ``_A2AProgressBridge._send`` mirrors the same gating.
  5. Both sites share the channel state instance — alternating
     dispatches accrue failures in one counter.
  6. After ``failure_threshold`` consecutive ``RETRYABLE_FAILURE``
     outcomes, the channel is dead; subsequent fires are skipped.
  7. A single ``SUCCESS`` resets the failure counter (= a transient
     5xx wave that recovers leaves the channel alive).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from reyn.chat.channel_state import (  # noqa: E402
    ChannelState,
    DeliveryOutcome,
    DeliveryResult,
)
from reyn.events.events import EventLog  # noqa: E402
from reyn.interfaces.web.a2a_intervention import A2AInterventionBus  # noqa: E402
from reyn.interfaces.web.run_registry import RunRegistry  # noqa: E402
from reyn.user_intervention import UserIntervention  # noqa: E402

# ── 1. RunRegistry.webhook_channel_state lifecycle ─────────────────────


def test_webhook_channel_state_returns_none_when_no_webhook_url() -> None:
    """Tier 2: peer that didn't register a webhook URL has no channel
    to track. ``webhook_channel_state`` returns None.
    """
    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A", webhook_url=None,
    )
    assert registry.webhook_channel_state(entry.run_id) is None


def test_webhook_channel_state_returns_none_for_unknown_run_id() -> None:
    """Tier 2: defensive — unknown run_id returns None, no KeyError."""
    registry = RunRegistry()
    assert registry.webhook_channel_state("no-such-run") is None


def test_webhook_channel_state_lazy_init_keyed_by_webhook_prefix() -> None:
    """Tier 2: first access on a RunEntry with a webhook URL creates
    a fresh ``ChannelState`` with ``channel_id="webhook:<run_id>"``.
    Subsequent calls return the same instance (= no re-init).
    """
    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )
    state_a = registry.webhook_channel_state(entry.run_id)
    state_b = registry.webhook_channel_state(entry.run_id)
    assert isinstance(state_a, ChannelState)
    assert state_a is state_b
    assert state_a.channel_id == f"webhook:{entry.run_id}"


# ── 2. A2AInterventionBus.on_dispatch gates on is_alive ────────────────


def test_a2a_bus_on_dispatch_skips_post_when_channel_dead(
    monkeypatch,
) -> None:
    """Tier 2: when the per-run ``ChannelState.is_alive()`` returns
    False (= prior dispatches accumulated ``failure_threshold``
    consecutive failures), ``on_dispatch`` does NOT call
    ``post_webhook``. The SSE history append still runs (= local
    sink, independent of webhook).
    """
    posted: list = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    # Pre-mark channel state dead via direct failure accumulation.
    state = registry.webhook_channel_state(entry.run_id)
    for _ in range(3):
        state.record_attempt(
            DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE),
        )
    assert not state.is_alive()

    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    # No webhook call attempted.
    assert posted == []
    # SSE buffer still appended (= local sink independent).
    assert len(registry.get(entry.run_id).history_events) == 1


def test_a2a_bus_on_dispatch_records_success(monkeypatch) -> None:
    """Tier 2: successful ``post_webhook`` updates the channel state
    via ``record_attempt`` — failures reset to 0, last_ack advances.
    """
    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    state = registry.webhook_channel_state(entry.run_id)
    assert state.delivery_failures == 0
    assert state.delivery_attempts_total == 1
    assert state.last_ack_at is not None
    assert state.is_alive()


def test_a2a_bus_on_dispatch_accumulates_consecutive_failures(
    monkeypatch,
) -> None:
    """Tier 2: ``RETRYABLE_FAILURE`` results from ``post_webhook``
    increment ``delivery_failures`` via ``record_attempt``. After
    ``failure_threshold`` consecutive failures the channel is dead
    and subsequent fires are skipped.
    """
    call_count = {"n": 0}

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        call_count["n"] += 1
        return DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        # 3 consecutive failures (= failure_threshold default).
        for _ in range(3):
            iv = UserIntervention(kind="ask_user", prompt="?")
            await bus.on_dispatch(iv)
        # 4th attempt: channel is dead, post_webhook NOT called.
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    state = registry.webhook_channel_state(entry.run_id)
    assert state.delivery_failures == 3  # threshold hit
    assert not state.is_alive()
    assert call_count["n"] == 3  # 4th call was skipped


def test_a2a_bus_on_dispatch_success_resets_failure_counter(
    monkeypatch,
) -> None:
    """Tier 2: a transient wave of failures followed by a success
    resets ``delivery_failures`` to 0 — the channel is alive again.
    """
    outcomes = [
        DeliveryOutcome.RETRYABLE_FAILURE,
        DeliveryOutcome.RETRYABLE_FAILURE,
        DeliveryOutcome.SUCCESS,
    ]
    call_count = {"n": 0}

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        outcome = outcomes[call_count["n"]]
        call_count["n"] += 1
        return DeliveryResult(outcome=outcome)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        for _ in range(3):
            iv = UserIntervention(kind="ask_user", prompt="?")
            await bus.on_dispatch(iv)

    asyncio.run(_drive())

    state = registry.webhook_channel_state(entry.run_id)
    # First two failures accrued, then SUCCESS reset to 0.
    assert state.delivery_failures == 0
    assert state.delivery_attempts_total == 3
    assert state.is_alive()


# ── 3. _A2AProgressBridge._send shares the same channel state ──────────


def test_progress_bridge_send_gates_on_is_alive(monkeypatch) -> None:
    """Tier 2: ``_A2AProgressBridge._send`` shares the per-run
    ChannelState with ``A2AInterventionBus``. A dead channel from
    earlier on_dispatch failures means progress events skip the
    webhook fire too.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    posted: list = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    # Pre-mark dead via the bus's accumulation.
    state = registry.webhook_channel_state(entry.run_id)
    for _ in range(3):
        state.record_attempt(
            DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE),
        )

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url="https://peer.test/hook",
        agent_name="demo",
        run_registry=registry,
    )

    asyncio.run(bridge._send(1, "phase_started", "phase: planning"))

    assert posted == [], (
        "bridge should skip webhook POST when shared ChannelState says dead"
    )
    # SSE buffer still appended.
    assert len(registry.get(entry.run_id).history_events) == 1


def test_bus_and_bridge_share_the_same_channel_state(monkeypatch) -> None:
    """Tier 2: both sites must update the SAME ``ChannelState``
    instance so an alternating sequence (= bus input-required, then
    bridge progress, then bus completed) feeds one consecutive-failure
    counter — not three separate counters that never hit threshold.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        return DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE)

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )

    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url="https://peer.test/hook",
        agent_name="demo",
        run_registry=registry,
    )

    async def _drive() -> None:
        # Bus fires 1 failure → state.delivery_failures = 1
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)
        # Bridge fires 1 failure → state.delivery_failures = 2 (shared)
        await bridge._send(1, "phase_started", "phase: planning")
        # Bus fires 1 more → state.delivery_failures = 3 = threshold
        iv2 = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv2)

    asyncio.run(_drive())

    state = registry.webhook_channel_state(entry.run_id)
    assert state.delivery_failures == 3, (
        f"bus + bridge must share ChannelState; got {state.delivery_failures} "
        f"(expected 3 from alternating 1+1+1 failures across both sites)"
    )
    assert not state.is_alive()


# Silence pytest unused-import; kept for parity / future extension.
_ = Path
