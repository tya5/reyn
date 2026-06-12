"""Tier 2: #1493 — A2A on_limit=interactive: iv dispatch → on_dispatch →
answer injection → resolve.

The #1493 fix removes the `on_limit=unattended` force in deps.py so that
A2A peer sessions use the operator-configured `on_limit` (default: interactive).
When the RouterLoop limit fires with `on_limit=interactive`, the iv is dispatched
through `_dispatch_intervention`, which calls `A2AInterventionBus.on_dispatch`
as a side-effect observer BEFORE awaiting the iv.future. The A2A peer receives
the input-required signal via SSE/webhook, answers via the A2A answer endpoint
(`answer_pending_intervention`), and the iv.future resolves → allow_continue.

Invariants pinned (each reflects one link in the #1493 mechanism):

1. `on_dispatch` mirrors status="input-required" on the RunEntry BEFORE the
   future is awaited (peer sees the prompt before the awaiter blocks).
2. After `on_dispatch`, iv.future is still pending (bus must NOT await it).
3. Injecting an answer via `iv.future.set_result` resolves the future
   (= allow_continue path).
4. The resolved answer carries choice_id="yes" (= loop-continue decision
   returned to the limit handler).

Real A2AInterventionBus + real RunRegistry — no mocks per policy.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.chat.session import ChatSession
from reyn.config import OnLimitConfig, SafetyConfig
from reyn.user_intervention import InterventionAnswer, UserIntervention
from reyn.web.a2a_intervention import A2AInterventionBus
from reyn.web.run_registry import RunRegistry


def _make_bus_and_registry(
    *,
    agent_name: str = "a2a_agent",
    webhook_url: str | None = None,
) -> tuple[A2AInterventionBus, RunRegistry, str]:
    """Return (bus, registry, run_id) with one RunEntry already created."""
    registry = RunRegistry()
    entry = registry.create(
        agent_name=agent_name,
        chain_id="test-chain",
        webhook_url=webhook_url,
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)
    return bus, registry, entry.run_id


@pytest.mark.asyncio
async def test_a2a_limit_on_dispatch_fires_before_future_awaited() -> None:
    """Tier 2: on_dispatch mirrors status="input-required" and returns
    promptly (does NOT await iv.future). Pins invariant 1+2.

    Represents: limit fires → _dispatch_intervention calls on_dispatch →
    peer sees "input-required" BEFORE the awaiter blocks on iv.future.
    """
    bus, registry, run_id = _make_bus_and_registry()
    iv = UserIntervention(kind="max_iterations_limit", prompt="Extend?", run_id=run_id)

    # on_dispatch must return without awaiting the future.
    await asyncio.wait_for(bus.on_dispatch(iv), timeout=2.0)

    # Invariant 1: status mirrored (peer can now poll "input-required").
    assert registry.get(run_id).status == "input-required"
    # Invariant 2: future still pending (bus must not consume it).
    assert not iv.future.done()


@pytest.mark.asyncio
async def test_a2a_limit_answer_injection_resolves_future() -> None:
    """Tier 2: after on_dispatch, injecting an answer via iv.future.set_result
    resolves the future correctly. Pins invariants 3+4.

    Represents: A2A peer POSTs answer → answer_pending_intervention delivers
    it → iv.future resolves with choice_id="yes" → allow_continue returned
    to the RouterLoop limit handler.
    """
    bus, registry, run_id = _make_bus_and_registry()
    iv = UserIntervention(kind="max_iterations_limit", prompt="Extend?", run_id=run_id)

    await bus.on_dispatch(iv)

    # Inject answer (simulates ChatSession.answer_pending_intervention path).
    answer = InterventionAnswer(text="yes", choice_id="yes")
    iv.future.set_result(answer)

    resolved = await iv.future
    # Invariant 3: future resolves.
    assert resolved is answer
    # Invariant 4: allow-continue choice propagated.
    assert resolved.choice_id == "yes"


@pytest.mark.asyncio
async def test_a2a_limit_on_dispatch_and_answer_concurrent() -> None:
    """Tier 2: on_dispatch fires and the future is resolved concurrently —
    the full #1493 seam: dispatch side-effect fires, peer answers while
    the await is live, resolve unblocks the awaiter.

    Pins the seam: on_dispatch (observer) + iv.future await (handler) +
    set_result (peer answer injection) are safe to race.
    """
    bus, registry, run_id = _make_bus_and_registry()
    iv = UserIntervention(kind="max_iterations_limit", prompt="Extend?", run_id=run_id)

    async def _peer_answer() -> None:
        # Simulate latency before peer answers.
        await asyncio.sleep(0.01)
        iv.future.set_result(InterventionAnswer(text="yes", choice_id="yes"))

    # Fire on_dispatch (side-effect observer), then start awaiting the future
    # while the peer answer task runs concurrently.
    await bus.on_dispatch(iv)
    _, resolved = await asyncio.gather(
        _peer_answer(),
        iv.future,
    )

    assert registry.get(run_id).status == "input-required"
    assert resolved.choice_id == "yes"


# ── Regression gate: factory seam must not force on_limit=unattended ──────────


def test_a2a_session_on_limit_threads_from_safety_config() -> None:
    """Tier 2: #1493 regression gate — the A2A session factory (deps.py) must
    pass config.safety unmodified so on_limit.mode is NOT forced to "unattended".

    ChatSession(safety=SafetyConfig(on_limit=OnLimitConfig(mode="interactive")))
    must expose session.on_limit.mode == "interactive". If deps.py re-introduces
    a `_dc.replace(config.safety, on_limit=OnLimitConfig(mode="unattended"))` force,
    A2A sessions would silently abort at every limit instead of dispatching iv to
    the peer — the 3 mechanism tests above would still pass while the regression
    is invisible.

    Scope note: the full web-layer _session_factory closure (deps._get_registry)
    is too heavy for isolation testing (requires AgentRegistry + ModelResolver +
    config-from-disk). This test covers the closest feasible seam: ChatSession
    constructor threads the provided SafetyConfig.on_limit without override.
    The deps.py seam is inspection-trivial (one `safety=config.safety` kwarg).
    """
    safety = SafetyConfig(on_limit=OnLimitConfig(mode="interactive"))
    session = ChatSession(agent_name="a2a-regression-test", safety=safety)
    assert session.on_limit.mode == "interactive"
