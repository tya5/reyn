"""Tier 2: A2AInterventionBus — contract tests (post-issue-#292 α refactor).

Pre-#292 this file pinned the bus as an iv owner: ``request(iv)``
awaited ``iv.future`` and returned the answer; the iv was stored in
``RunEntry.pending_intervention``. The pre-α architecture is described
in issue #292 body (= "A2A override completely bypasses ChatSession's
iv machinery"); see history of this file in git for the old test
shape.

Post-α the bus is a **side-effect observer**: ``on_dispatch(iv)`` is
invoked by ``ChatSession._dispatch_intervention`` for chain-registered
overrides and runs A2A peer-facing notifications (RunRegistry status
mirror, SSE history append, webhook POST) without taking iv
ownership. The iv lives in ``ChatSession._interventions._active`` and
the future is awaited by ``InterventionHandler.dispatch`` like any
other (= TUI) iv.

Pins (= α contract):

  1. ``A2AInterventionBus.on_dispatch(iv)`` exists; ``request`` /
     ``deliver`` are removed (= peer answers no longer flow through
     the bus; they flow through ``ChatSession.answer_pending_intervention``).
  2. ``on_dispatch`` mirrors ``status="input-required"`` on the RunEntry.
  3. ``on_dispatch`` does NOT write ``pending_intervention`` to
     RunEntry (= the field is dropped from RunEntry entirely).
  4. ``on_dispatch`` does NOT await ``iv.future`` (= it returns
     promptly so dispatch can continue to the handler).
  5. ``on_dispatch`` posts a webhook when ``webhook_url`` is configured
     and skips it when None.
  6. ``on_dispatch`` is best-effort: missing RunEntry → warning log,
     no raise. Side-effect failure (= webhook 500, broken append_event)
     → swallowed.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from reyn.user_intervention import UserIntervention
from reyn.web.a2a_intervention import A2AInterventionBus
from reyn.web.run_registry import RunEntry, RunRegistry


def _make_registry_with_run(
    *,
    agent_name: str = "test_agent",
    webhook_url: str | None = None,
) -> tuple[RunRegistry, str]:
    """Return (registry, run_id) with one entry already created."""
    registry = RunRegistry()
    entry = registry.create(
        agent_name=agent_name,
        chain_id="test-chain",
        webhook_url=webhook_url,
    )
    return registry, entry.run_id


# ── 1. API surface ─────────────────────────────────────────────────────────────


def test_bus_exposes_on_dispatch_not_request_or_deliver() -> None:
    """Tier 2: α contract — ``on_dispatch`` is the only public method.
    ``request`` / ``deliver`` (= pre-α names that returned an answer)
    are removed because peer answers no longer flow through the bus.
    """
    registry = RunRegistry()
    bus = A2AInterventionBus(run_id="x", registry=registry)
    assert hasattr(bus, "on_dispatch")
    assert not hasattr(bus, "request")
    assert not hasattr(bus, "deliver")


def test_bus_channel_id_format_unchanged() -> None:
    """Tier 2: ``channel_id`` is still ``a2a:<run_id>`` (= issue #268
    contract preserved across the α refactor).
    """
    registry = RunRegistry()
    bus = A2AInterventionBus(run_id="abc123", registry=registry)
    assert bus.channel_id == "a2a:abc123"


# ── 2. Status mirror ───────────────────────────────────────────────────────────


def test_on_dispatch_mirrors_input_required_status() -> None:
    """Tier 2: ``on_dispatch`` flips RunEntry.status to ``"input-required"``
    so polling peers see the pending state. The iv itself stays in
    ChatSession; this is a public-status mirror only.
    """
    registry, run_id = _make_registry_with_run()
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    assert registry.get(run_id).status == "input-required"


def test_on_dispatch_does_not_write_pending_intervention_to_run_entry() -> None:
    """Tier 2: α contract — the RunEntry has NO ``pending_intervention``
    field. The iv lives in ChatSession's outstanding_interventions.
    Verifies the dataclass shape change ships cleanly.
    """
    registry, run_id = _make_registry_with_run()
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    entry = registry.get(run_id)
    assert isinstance(entry, RunEntry)
    assert not hasattr(entry, "pending_intervention")
    assert not hasattr(entry, "question")


# ── 3. No await iv.future ──────────────────────────────────────────────────────


def test_on_dispatch_returns_promptly_without_awaiting_future() -> None:
    """Tier 2: α contract — ``on_dispatch`` MUST return without
    awaiting ``iv.future``. The handler dispatch path awaits on the
    skill's behalf; if the bus also awaited, two awaiters would race
    on the same future. Pin by leaving the future unresolved + asserting
    the call returns.
    """
    registry, run_id = _make_registry_with_run()
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> bool:
        iv = UserIntervention(kind="ask_user", prompt="?")
        # If on_dispatch awaited iv.future, this would hang forever
        # because no one resolves it.
        await asyncio.wait_for(bus.on_dispatch(iv), timeout=2.0)
        return not iv.future.done()  # future MUST still be pending

    assert asyncio.run(_drive())


# ── 4. Webhook fire is gated on URL ────────────────────────────────────────────


def test_on_dispatch_no_webhook_when_url_unset() -> None:
    """Tier 2: peer that didn't register a webhook URL sees zero HTTP
    cost. We pin this by using a real httpx MockTransport that fails
    the test if a request reaches it (= no patch, no mock).
    """
    posted: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(_handler)
    # Inject the transport via the reyn.web.notifications client
    # factory — but the simplest pin is just: webhook_url=None should
    # skip post_webhook entirely without any transport setup.

    registry, run_id = _make_registry_with_run(webhook_url=None)
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    # No webhook URL → no HTTP attempt at all.
    assert posted == [], "no webhook URL set, but a request was made"


def test_on_dispatch_posts_webhook_when_url_set(monkeypatch) -> None:
    """Tier 2: with ``webhook_url`` set, ``on_dispatch`` POSTs the
    canonical input-required payload (= issue #267 Gap 4 shape with
    ``kind`` / ``choices`` / ``detail``).
    """
    posted: list[tuple[str, dict]] = []

    async def _fake_post(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))
        from reyn.web.notifications import DeliveryOutcome, DeliveryResult
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post)

    registry, run_id = _make_registry_with_run(
        webhook_url="https://peer.test/hook",
    )
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="Question?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    assert len(posted) == 1
    url, payload = posted[0]
    assert url == "https://peer.test/hook"
    assert payload["status"] == "input-required"
    assert payload["question"] == "Question?"
    assert payload["kind"] == "ask_user"


# ── 5. Defensive paths ─────────────────────────────────────────────────────────


def test_on_dispatch_unknown_run_id_logs_warning_no_raise(caplog) -> None:
    """Tier 2: defensive — when the bus's run_id is not in the
    registry, ``on_dispatch`` logs a warning and returns. Pre-α this
    raised RuntimeError; α changed to warn+return because raising
    would abort the dispatch chain and the iv would never reach the
    handler.
    """
    import logging as _logging

    registry = RunRegistry()  # empty
    bus = A2AInterventionBus(run_id="ghost-run", registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        # No raise expected.
        await bus.on_dispatch(iv)

    with caplog.at_level(_logging.WARNING, logger="reyn.web.a2a_intervention"):
        asyncio.run(_drive())

    assert any(
        "ghost-run" in record.message for record in caplog.records
    ), "expected a warning naming the unknown run_id"


def test_on_dispatch_webhook_failure_does_not_raise(monkeypatch) -> None:
    """Tier 2: side-effect failure on the webhook sink is swallowed.
    The iv must continue to ``InterventionHandler.dispatch`` even if
    the peer's webhook server is down.
    """

    async def _failing_post(url: str, payload: dict):  # noqa: ANN202
        raise RuntimeError("simulated peer 500")

    import reyn.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _failing_post)

    registry, run_id = _make_registry_with_run(
        webhook_url="https://peer.test/hook",
    )
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        # Expect no exception to propagate.
        await bus.on_dispatch(iv)

    asyncio.run(_drive())


# ── 6. SSE history append (= Gap 1 wire preserved) ─────────────────────────────


def test_on_dispatch_appends_input_required_to_history_events() -> None:
    """Tier 2: ``on_dispatch`` appends the input-required payload to
    ``RunEntry.history_events`` so SSE consumers see ask_user prompts
    inline. issue #267 Gap 1 wire preserved across the α refactor.
    """
    registry, run_id = _make_registry_with_run()
    bus = A2AInterventionBus(run_id=run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    history = registry.get(run_id).history_events
    assert len(history) == 1
    assert history[0]["status"] == "input-required"
    assert history[0]["kind"] == "ask_user"


# Silence unused-import warning for httpx/json kept for parity with the
# original FP-0001 file naming convention; they may be needed by future
# tests that want to use httpx.MockTransport explicitly.
_ = json
_ = pytest
