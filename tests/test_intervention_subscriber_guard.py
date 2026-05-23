"""Tier 2: InterventionRegistry subscriber-presence guard (issue #254 Phase 1).

When ``enforce_listener_presence=True`` is set at construction AND no
listener is registered, ``dispatch()`` returns an empty
``InterventionAnswer`` immediately instead of awaiting an unresolvable
future. This is the structural fix that unblocks ``safety.on_limit.mode``
default = ``interactive`` + ``ask_timeout_seconds=0`` (= "wait forever
for a human reply") on test / headless paths where no UI listener is
attached.

Pins:
  1. Default (``enforce_listener_presence=False``) preserves legacy
     forever-await behaviour (= what existing low-level registry tests
     rely on).
  2. Enforced mode with zero listeners short-circuits to empty answer.
  3. Enforced mode with one+ listeners awaits as normal.
  4. Register / unregister round-trip restores short-circuit behaviour.
  5. ChatSession constructs the registry in enforced mode and exposes
     ``register_intervention_listener`` / ``unregister_intervention_listener``
     wrappers.
  6. End-to-end: a ChatSession that hits a safety limit under
     ``interactive`` + ``ask_timeout_seconds=0`` with no listener returns
     immediately (no hang) and lets the caller fall through to its
     legacy abort path.

No mocks. Real registry, real ChatSession.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.services.intervention_registry import InterventionRegistry
from reyn.chat.session import ChatSession
from reyn.config import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── 1. Default (legacy) — no enforcement ────────────────────────────────


def test_default_constructor_does_not_enforce_listener_presence() -> None:
    """Tier 2: default ``enforce_listener_presence=False`` preserves
    the pre-issue-#254 contract — dispatch awaits the future regardless
    of listener registration.

    Direct-registry tests (= ``test_intervention_handler_invariants.py``
    et al) construct registries with no listener and rely on this
    forever-await semantics; opt-in flag preserves them.
    """
    announce_calls: list[str] = []

    async def _announce(iv: UserIntervention) -> None:
        announce_calls.append(iv.id)

    reg = InterventionRegistry(on_announce=_announce)
    assert reg.listener_count() == 0
    assert reg.has_active_listener() is False

    # In default mode, dispatch will enqueue + announce + await. Without
    # an external resolver, the await would block forever — so we only
    # verify that the dispatch DID enqueue rather than short-circuiting.
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    iv.future = asyncio.get_event_loop().create_future() \
        if hasattr(asyncio, "get_event_loop") else None

    async def _drive() -> None:
        loop = asyncio.get_running_loop()
        iv.future = loop.create_future()
        task = asyncio.ensure_future(reg.dispatch(iv))
        # Let the dispatch coroutine reach its await.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Verify it enqueued (= did NOT short-circuit).
        assert reg.queued_count() == 1, (
            "default mode must enqueue + await, not short-circuit"
        )
        # Resolve the future manually so the task can clean up.
        iv.future.set_result(InterventionAnswer(text="ok"))
        answer = await task
        assert answer.text == "ok"
        assert announce_calls == [iv.id], "announce must fire in default mode"

    asyncio.run(_drive())


# ── 2. Enforced mode with zero listeners short-circuits ────────────────


def test_enforced_mode_with_no_listener_short_circuits_with_empty_answer() -> None:
    """Tier 2: when enforcement is on AND no listener is registered,
    dispatch returns ``InterventionAnswer(text="")`` immediately —
    matching the cancellation contract so callers fall through to
    refusal / abort.
    """
    async def _announce(iv: UserIntervention) -> None:
        pytest.fail("announce must NOT fire when no listener short-circuits")

    reg = InterventionRegistry(
        on_announce=_announce, enforce_listener_presence=True,
    )
    iv = UserIntervention(kind="ask_user", prompt="Q?")

    async def _drive() -> InterventionAnswer:
        return await reg.dispatch(iv)

    answer = asyncio.run(_drive())
    assert isinstance(answer, InterventionAnswer)
    assert answer.text == ""
    assert answer.choice_id is None
    # Queue must be left empty — short-circuit happens BEFORE enqueue.
    assert reg.queued_count() == 0


# ── 3. Enforced mode with a listener awaits as normal ──────────────────


def test_enforced_mode_with_registered_listener_dispatches_normally() -> None:
    """Tier 2: with at least one listener registered, dispatch behaves
    identically to the default (= enqueue + announce + await for
    deliver_answer to resolve).
    """
    announce_calls: list[str] = []

    async def _announce(iv: UserIntervention) -> None:
        announce_calls.append(iv.id)

    reg = InterventionRegistry(
        on_announce=_announce, enforce_listener_presence=True,
    )
    reg.register_listener("tui")
    assert reg.has_active_listener() is True
    assert reg.listener_count() == 1

    iv = UserIntervention(kind="ask_user", prompt="Q?")

    async def _drive() -> None:
        loop = asyncio.get_running_loop()
        iv.future = loop.create_future()
        task = asyncio.ensure_future(reg.dispatch(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert reg.queued_count() == 1
        assert announce_calls == [iv.id]
        # Resolve so the task can finish.
        ok = await reg.deliver_answer(iv, "hi")
        assert ok is True
        result = await task
        assert result.text == "hi"

    asyncio.run(_drive())


# ── 4. Register / unregister round-trip ────────────────────────────────


def test_register_unregister_listener_round_trip() -> None:
    """Tier 2: register adds, unregister removes, both are idempotent
    and ``has_active_listener`` tracks the set's emptiness.
    """
    async def _announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(
        on_announce=_announce, enforce_listener_presence=True,
    )

    # Initial: empty.
    assert reg.has_active_listener() is False

    # Register two distinct listeners.
    reg.register_listener("tui")
    reg.register_listener("slash")
    assert reg.listener_count() == 2
    assert reg.has_active_listener() is True

    # Re-register same id: idempotent.
    reg.register_listener("tui")
    assert reg.listener_count() == 2

    # Unregister one.
    reg.unregister_listener("tui")
    assert reg.listener_count() == 1
    assert reg.has_active_listener() is True

    # Unregister unknown: idempotent (no raise).
    reg.unregister_listener("nonexistent")
    assert reg.listener_count() == 1

    # Unregister the last one: short-circuit re-enabled.
    reg.unregister_listener("slash")
    assert reg.has_active_listener() is False


def test_unregistering_after_register_re_enables_short_circuit() -> None:
    """Tier 2: dispatch short-circuits again once the last listener
    unregisters (= "TUI unmounted, no one is listening anymore").
    """
    async def _announce(iv: UserIntervention) -> None:
        return None

    reg = InterventionRegistry(
        on_announce=_announce, enforce_listener_presence=True,
    )
    reg.register_listener("tui")
    reg.unregister_listener("tui")

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    answer = asyncio.run(reg.dispatch(iv))
    assert answer.text == ""


# ── 5. ChatSession exposes the wrappers and opts into enforcement ──────


def test_chat_session_constructs_registry_in_enforced_mode(tmp_path: Path) -> None:
    """Tier 2: a fresh ChatSession has the registry in enforcement mode
    AND starts with zero listeners (= test / headless contexts get the
    short-circuit by default).
    """
    session = ChatSession(agent_name="test_agent")
    # Internal attribute access is acceptable here because we are pinning
    # the wiring invariant the entry-point relies on. The public surface
    # ``register_intervention_listener`` is what callers use.
    assert session._interventions.is_listener_enforcement_enabled() is True
    assert session._interventions.has_active_listener() is False


def test_chat_session_register_intervention_listener_round_trip() -> None:
    """Tier 2: ChatSession's public register / unregister thin wrappers
    update the registry's listener set.
    """
    session = ChatSession(agent_name="test_agent")

    session.register_intervention_listener("tui")
    assert session._interventions.has_active_listener() is True

    session.unregister_intervention_listener("tui")
    assert session._interventions.has_active_listener() is False


# ── 6. End-to-end: limit hit under interactive + timeout=0 + no listener ──


def test_safety_limit_under_interactive_no_timeout_no_listener_no_hang(
    tmp_path: Path,
) -> None:
    """Tier 2: the exact scenario that motivated issue #254 — a chat
    session hits a safety limit while running ``mode=interactive`` +
    ``ask_timeout_seconds=0``, with no UI listener attached. Pre-Phase 1
    this hung indefinitely awaiting the future. Phase 1: dispatch
    short-circuits, ``handle_limit_exceeded`` sees an empty answer,
    treats it as refusal, the caller raises ``RouterCapExceeded``, and
    ``_handle_user_message`` emits its fallback message and returns.

    The test asserts the run completes within a small wall-clock budget
    — a 5s ceiling is generous; pre-fix the same path waited the entire
    pytest-timeout window.
    """
    safety = SafetyConfig(
        loop=LoopConfig(max_router_calls_per_turn=3),
        on_limit=OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0),
    )
    session = ChatSession(
        agent_name="test_agent",
        output_language="ja",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )
    # DELIBERATELY do not register a listener — that is the test condition.

    # Pre-spend the router budget so the next call hits the cap.
    session._router_invocations_this_turn = 3
    session._router_last_reason = "out_of_scope"

    async def _drive() -> None:
        # Must complete promptly — pre-Phase 1 this awaited forever.
        await asyncio.wait_for(
            session._handle_user_message("こんにちは", chain_id="chain-no-listener"),
            timeout=5.0,
        )

    asyncio.run(_drive())
