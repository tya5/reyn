"""Tier 2: Agent (Session) as RequestBus subscriber (issue #254 Phase 3).

Pins Phase 3's behaviour-parity introduction of the Agent-layer
intervention handler:

  - ``Session.handle_intervention(iv)`` is the Agent's entry point
    for incoming intervention requests. Phase 3 ships pure forward-only
    behaviour (= delegates to ``_dispatch_intervention``); Phase 4 will
    add ``self_answer`` / ``parent_delegate`` branches without changing
    this surface.
  - ``Session.as_request_bus()`` returns an ``AgentRequestBus``
    adapter that satisfies the ``RequestBus`` Protocol from Phase 2.
    OS-layer callers (= ``handle_limit_exceeded``, permission gates,
    ``ask_user`` op) hold this adapter without importing Session.
  - ``AgentRequestBus.request(iv)`` forwards to
    ``Session.handle_intervention(iv)`` — pinning the wire from
    OS [A] contract → Agent layer → downstream channel selection.

End-to-end behaviour parity is verified via the existing dispatch path
(= the same ``_dispatch_intervention`` that PR #255 / #258 covered);
Phase 3 only adds the Agent-layer NAME for the same code path, so
existing subscriber-guard / outbox / chain-override semantics are
re-verified through the new entry-point.

No mocks. Real Session, real adapter.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from reyn.runtime.session import Session
from reyn.runtime.session_buses import AgentRequestBus
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    RequestBus,
    UserIntervention,
)

# ── 1. Session.handle_intervention exists and is the canonical
#      Agent-layer entry point ────────────────────────────────────────────


def test_chat_session_has_handle_intervention_method() -> None:
    """Tier 2: Session exposes ``handle_intervention(iv)`` as the
    Agent's intervention entry point.
    """
    assert hasattr(Session, "handle_intervention")
    method = Session.handle_intervention
    assert inspect.iscoroutinefunction(method)


def test_handle_intervention_signature_is_iv_to_answer() -> None:
    """Tier 2: signature is ``handle_intervention(self, iv) -> ...``.

    Pinning the shape so a future refactor cannot quietly change the
    contract from underneath OS callers.
    """
    sig = inspect.signature(Session.handle_intervention)
    params = list(sig.parameters.keys())
    assert params == ["self", "iv"]


def test_handle_intervention_forwards_to_dispatch_intervention() -> None:
    """Tier 2: Phase 3 ships behaviour parity — the Agent layer just
    forwards to the existing ``_dispatch_intervention`` path so the
    chain-override / TUI fall-through routing keeps working unchanged.

    Verified via source inspection (= the body MUST delegate to
    ``self._dispatch_intervention(iv)`` in Phase 3). Phase 4 will add
    self_answer / parent_delegate branches, at which point this test
    is updated to assert the forward-path remains as one of multiple
    branches.
    """
    src = inspect.getsource(Session.handle_intervention)
    assert "self._dispatch_intervention(iv)" in src, (
        "Phase 3 handle_intervention must delegate to "
        "_dispatch_intervention for behaviour parity"
    )


# ── 2. AgentRequestBus adapter satisfies the RequestBus protocol ───────


def test_agent_request_bus_satisfies_request_bus_protocol() -> None:
    """Tier 2: an AgentRequestBus instance satisfies the runtime_checkable
    RequestBus Protocol — OS callers typed against ``bus: RequestBus``
    accept it directly.
    """
    session = Session(agent_name="t")
    bus = AgentRequestBus(session)
    assert isinstance(bus, RequestBus)


def test_agent_request_bus_also_satisfies_legacy_intervention_bus_alias() -> None:
    """Tier 2: backwards-compat — OS callers typed against the legacy
    ``InterventionBus`` name still accept an AgentRequestBus because
    ``InterventionBus`` is an alias of ``RequestBus`` (Phase 2 invariant).
    """
    session = Session(agent_name="t")
    bus = AgentRequestBus(session)
    assert isinstance(bus, InterventionBus)


def test_agent_request_bus_request_signature_is_iv_to_answer() -> None:
    """Tier 2: AgentRequestBus.request shape is ``(iv) -> InterventionAnswer``."""
    sig = inspect.signature(AgentRequestBus.request)
    params = list(sig.parameters.keys())
    assert params == ["self", "iv"]


# ── 3. as_request_bus() factory returns an AgentRequestBus ─────────────


def test_as_request_bus_returns_agent_request_bus() -> None:
    """Tier 2: Session.as_request_bus() returns an AgentRequestBus
    bound to this session (= the canonical way for OS callers to get a
    RequestBus-typed reference without importing AgentRequestBus directly).
    """
    session = Session(agent_name="t")
    bus = session.as_request_bus()
    assert isinstance(bus, AgentRequestBus)
    assert isinstance(bus, RequestBus)


def test_as_request_bus_returns_fresh_adapter_each_call() -> None:
    """Tier 2: as_request_bus() is a factory — each call returns a new
    adapter (= no global state, no caching surprises).

    The adapters are equivalent (all forward to the same session.handle_intervention)
    but distinct objects so OS callers can safely hold their own.
    """
    session = Session(agent_name="t")
    bus1 = session.as_request_bus()
    bus2 = session.as_request_bus()
    assert bus1 is not bus2
    # Both reference the same session.
    assert bus1.session is bus2.session is session


# ── 4. End-to-end: AgentRequestBus.request → session.handle_intervention
#      → existing dispatch path (= behaviour parity) ───────────────────


def test_agent_request_bus_request_reaches_session_handle_intervention(
    tmp_path: Path,
) -> None:
    """Tier 2: OS-layer call to ``agent_request_bus.request(iv)`` reaches
    ``session.handle_intervention(iv)`` which reaches the existing
    ``_dispatch_intervention`` path — confirms the wire end-to-end.

    Uses the Phase 1 subscriber-presence guard: no listener registered,
    so ``_dispatch_intervention`` short-circuits to an empty answer.
    The test asserts the short-circuit IS observed via the new entry
    point — proving the wiring is complete.
    """
    session = Session(agent_name="t")
    # Deliberately no register_intervention_listener — Phase 1 guard
    # short-circuits the dispatch, returning empty answer.
    bus = session.as_request_bus()
    iv = UserIntervention(kind="ask_user", prompt="Q?")

    answer = asyncio.run(bus.request(iv))
    assert isinstance(answer, InterventionAnswer)
    # Phase 1 short-circuit returns empty answer.
    assert answer.text == ""
    assert answer.choice_id is None


def test_agent_request_bus_request_with_registered_listener_round_trip(
    tmp_path: Path,
) -> None:
    """Tier 2: with a listener registered, the request flows through
    handle_intervention → _dispatch_intervention → registry, and a
    test-driven deliver_answer resolves the future.

    Verifies the Agent-layer entry point doesn't bypass the registry —
    the same dispatch path used pre-Phase-3 keeps working when invoked
    through the new RequestBus surface.
    """
    session = Session(agent_name="t")
    session.register_intervention_listener("test")

    bus = session.as_request_bus()

    async def _drive() -> InterventionAnswer:
        # Construct iv inside the running loop so its Future binds to
        # the same loop the registry awaits in.
        iv = UserIntervention(kind="ask_user", prompt="Q?")
        # Start the request in the background and resolve it via the
        # session's existing deliver_answer surface — same shape that
        # session-intervention tests use.
        task = asyncio.ensure_future(bus.request(iv))
        # Yield twice so the registry enqueues + waits.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ok = await session._deliver_answer_to(iv, "hello")
        assert ok is True
        return await task

    answer = asyncio.run(_drive())
    assert answer.text == "hello"


# ── 5. session.interventions attribute path is stable (tui-coder Q2) ──


def test_session_interventions_attribute_path_is_stable_in_phase3() -> None:
    """Tier 2: Session._interventions remains the canonical reference
    to the InterventionRegistry — tui-coder Q2 commitment from issue
    #254 alignment.

    Phase 3 introduces the Agent-layer entry point + AgentRequestBus
    adapter but does NOT move ownership of the registry. TUI continues
    to read ``session.interventions.queued_count()`` etc. without any
    import / call site change. If a future phase moves the registry into
    a sub-component, ``session.interventions`` must remain a proxy.
    """
    from reyn.runtime.services.intervention_registry import InterventionRegistry

    session = Session(agent_name="t")
    assert hasattr(session, "_interventions")
    assert isinstance(session.interventions, InterventionRegistry)
    # The registry is still enforcing the Phase 1 subscriber guard.
    assert session.interventions.is_listener_enforcement_enabled() is True


# ── 6. handle_intervention preserves the chain-override path ───────────


def test_handle_intervention_notifies_chain_override_observer(tmp_path: Path) -> None:
    """Tier 2: (= issue #292 α): when a chain override is registered,
    ``handle_intervention`` notifies it via ``on_dispatch`` AS A
    SIDE EFFECT before continuing through the regular handler. The
    iv future is owned by the handler post-α; the override is a
    pure observer.
    """
    session = Session(agent_name="t")
    session.register_intervention_listener("test")

    captured: list[UserIntervention] = []

    class _StubOverrideObserver:
        async def on_dispatch(self, iv: UserIntervention) -> None:
            captured.append(iv)

    session.register_intervention_override("chain-X", _StubOverrideObserver())
    session.running_skills_chain["run-1"] = "chain-X"

    async def _drive() -> tuple[UserIntervention, InterventionAnswer]:
        iv = UserIntervention(kind="ask_user", prompt="Q?", run_id="run-1")

        async def _resolve() -> None:
            await asyncio.sleep(0.05)
            iv.future.set_result(InterventionAnswer(text="from-handler"))

        resolver = asyncio.create_task(_resolve())
        try:
            answer = await session.handle_intervention(iv)
        finally:
            resolver.cancel()
        return iv, answer

    iv, answer = asyncio.run(_drive())

    # α: observer received the iv as a side effect; handler resolved the future.
    assert len(captured) > 0
    assert captured[0] is iv
    assert answer.text == "from-handler"
