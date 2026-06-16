"""Tier 2: A2A SSE producer wiring (issue #267 Gap 1).

Before this PR, ``GET /a2a/tasks/{run_id}/events`` (= SSE stream)
yielded only the terminal ``event: end`` because no in-tree code called
``RunRegistry.append_event``. Peer connections received empty streams
plus a single end-of-task signal — polling-equivalent with extra
infrastructure.

This PR makes two callers feed ``history_events``:

  1. ``_A2AProgressBridge._send`` — phase_started / llm_called /
     act_executed events fan out to BOTH the SSE buffer + (optionally)
     the webhook POST. Bridge is now constructed unconditionally in
     ``_handle_async_mode._run`` (= the webhook_url gate moved INTO
     the bridge's per-sink dispatch).
  2. ``A2AInterventionBus.deliver`` — the input-required payload is
     appended to history_events BEFORE the optional webhook POST. SSE
     consumers see ask_user prompts inline with the progress stream.

Pins:

  1. Bridge constructor accepts ``webhook_url=None`` + ``run_registry``.
  2. ``_send`` appends payload to history_events on every fire.
  3. ``_send`` only POSTs webhook when ``webhook_url`` is non-None.
  4. SSE sink failure does NOT block webhook fire (and vice versa).
  5. ``A2AInterventionBus.deliver`` appends input-required to
     history_events when prompted.
  6. Terminal events (completed / failed) still bypass history_events
     (= SSE endpoint yields ``event: end`` on terminal status directly,
     no double-emission).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from reyn.events.events import EventLog  # noqa: E402
from reyn.interfaces.web.a2a_intervention import A2AInterventionBus  # noqa: E402
from reyn.interfaces.web.run_registry import RunRegistry  # noqa: E402
from reyn.user_intervention import (  # noqa: E402
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)

# ── 1. Bridge constructor accepts webhook_url=None ────────────────────


def test_bridge_constructor_accepts_optional_webhook_url() -> None:
    """Tier 2: ``_A2AProgressBridge`` accepts ``webhook_url=None``
    (= the SSE-only path) without raising. Pre-Gap-1 the bridge was
    only built when webhook_url was set, so this signature flip is the
    contract that enables SSE-only deployments.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id="run-N",
        webhook_url=None,  # ← optional now
        agent_name="demo",
        run_registry=RunRegistry(),
    )
    assert bridge.webhook_url is None


# ── 2. _send appends to history_events ────────────────────────────────


def test_send_appends_payload_to_history_events() -> None:
    """Tier 2: ``_send`` calls ``run_registry.append_event(run_id,
    payload)`` for every fire. This is the SSE producer wiring (= Gap 1
    core): without it, history_events stays empty + the SSE stream
    yields nothing.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url=None,
        agent_name="demo",
        run_registry=registry,
    )

    asyncio.run(bridge._send(3, "phase_started", "phase: planning"))

    refreshed = registry.get(entry.run_id)
    (appended,) = refreshed.history_events
    assert appended == {
        "run_id": entry.run_id,
        "status": "in-progress",
        "progress": 3,
        "event": "phase_started",
        "message": "phase: planning",
        "agent_name": "demo",
    }


# ── 3. _send only POSTs webhook when configured ───────────────────────


def test_send_skips_webhook_when_webhook_url_is_none(monkeypatch) -> None:
    """Tier 2: with ``webhook_url=None``, ``_send`` does NOT call
    ``post_webhook`` (= the SSE-only deployment pays zero HTTP cost).
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    posted: list = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id="run-N",
        webhook_url=None,
        agent_name="demo",
        run_registry=RunRegistry(),
    )

    asyncio.run(bridge._send(1, "phase_started", "phase: planning"))
    assert posted == []


def test_send_posts_webhook_when_webhook_url_set(monkeypatch) -> None:
    """Tier 2: with ``webhook_url`` set, ``_send`` POSTs the SAME
    payload that was appended to history_events. Pin both side
    effects so the SSE consumer + webhook consumer see consistent
    state.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    posted: list[tuple[str, dict]] = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url="https://peer.test/hook",
        agent_name="demo",
        run_registry=registry,
    )

    asyncio.run(bridge._send(2, "llm_called", "llm: m"))

    # SSE sink
    sse = registry.get(entry.run_id).history_events
    # Webhook sink
    (posted_item,) = posted
    posted_url, posted_payload = posted_item
    # Both sinks saw the SAME payload
    assert sse[0] == posted_payload
    assert posted_url == "https://peer.test/hook"


# ── 4. Sink independence ──────────────────────────────────────────────


def test_sse_sink_failure_does_not_block_webhook(monkeypatch) -> None:
    """Tier 2: if ``run_registry.append_event`` raises, ``_send`` still
    fires the webhook. Each sink swallows its own failure
    independently.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    class _RaisingRegistry:
        def append_event(self, run_id, event):
            raise RuntimeError("simulated sse buffer failure")

    posted: list = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id="run-N",
        webhook_url="https://peer.test/hook",
        agent_name="demo",
        run_registry=_RaisingRegistry(),
    )

    asyncio.run(bridge._send(1, "phase_started", "phase: planning"))
    # webhook still ran despite SSE failure
    (only,) = posted


def test_webhook_sink_failure_does_not_block_sse(monkeypatch) -> None:
    """Tier 2: inverse — if ``post_webhook`` raises, the SSE append
    already happened (= sequential, SSE first then webhook). Confirms
    independence in both directions.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    async def _failing_post(url: str, payload: dict):  # noqa: ANN202
        raise RuntimeError("simulated webhook failure")

    import reyn.interfaces.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _failing_post)

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url="https://peer.test/hook",
        agent_name="demo",
        run_registry=registry,
    )

    asyncio.run(bridge._send(1, "act_executed", "act: 2 ops"))

    # SSE append succeeded despite webhook failure
    assert len(registry.get(entry.run_id).history_events) == 1


# ── 5. A2AInterventionBus.deliver appends input-required ──────────────


def test_a2a_intervention_bus_on_dispatch_appends_input_required_to_history() -> None:
    """Tier 2: ``A2AInterventionBus.on_dispatch`` appends the
    input-required payload to RunEntry.history_events. SSE consumers
    see ask_user prompts inline with the progress stream. issue #292
    α: renamed from ``deliver`` to ``on_dispatch``.
    """
    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url=None,  # no webhook = SSE-only consumer
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(
            kind="permission.confirm",
            prompt="Allow read?",
            choices=[InterventionChoice(id="yes", label="[Y]", hotkey="y")],
        )
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    refreshed = registry.get(entry.run_id)
    (appended,) = refreshed.history_events
    assert appended["status"] == "input-required"
    assert appended["kind"] == "permission.confirm"
    assert appended["question"] == "Allow read?"
    assert appended["choices"] == [
        {"id": "yes", "label": "[Y]", "hotkey": "y"},
    ]


def test_a2a_intervention_bus_appends_history_even_without_webhook() -> None:
    """Tier 2: ``A2AInterventionBus.deliver`` appends to history_events
    even when ``webhook_url`` is None on the RunEntry (= the SSE-only
    peer code path). Pre-Gap-1, no history was written at all in this
    case → SSE stream stayed empty → the streaming claim was dead.
    """
    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url=None,
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())
    assert len(registry.get(entry.run_id).history_events) == 1


# ── 6. SSE endpoint replay sanity ─────────────────────────────────────


def test_sse_endpoint_replays_appended_events_via_history() -> None:
    """Tier 2: end-to-end sanity — events appended via the bridge are
    visible through ``stream_task_events``'s history_events read. We
    don't drive the StreamingResponse generator (= would need a full
    ASGI stack); instead pin that the data flow lands where the
    endpoint reads from.
    """
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id=entry.run_id,
        webhook_url=None,
        agent_name="demo",
        run_registry=registry,
    )

    async def _drive() -> None:
        await bridge._send(1, "phase_started", "phase: planning")
        await bridge._send(2, "llm_called", "llm: m")
        await bridge._send(3, "act_executed", "act: 2 ops")

    asyncio.run(_drive())

    refreshed = registry.get(entry.run_id)
    # The endpoint code at line 921 reads `entry.history_events[seen:]`
    # — confirm the slice the endpoint will replay has 3 ordered events.
    assert [e["event"] for e in refreshed.history_events] == [
        "phase_started", "llm_called", "act_executed",
    ]
    assert [e["progress"] for e in refreshed.history_events] == [1, 2, 3]
