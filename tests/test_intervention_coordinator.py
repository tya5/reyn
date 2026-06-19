"""Tier 2: InterventionCoordinator owns override state + dispatch orchestration.

Pins the three behaviours that relocated from Session into the coordinator
(#1792 intervention seam): override-observer side-effect (best-effort, fires
before dispatch), origin-pin stall (absent listener → parked), and the
``is_override_active`` predicate the bus consults. Uses a real
InterventionRegistry + small fakes (no mocks).
"""
import asyncio

import pytest

from reyn.runtime.services.intervention_coordinator import InterventionCoordinator
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _Events:
    """Records emit() calls."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name: str, **kw) -> None:
        self.emitted.append((name, kw))


class _Override:
    """Override observer fake — records on_dispatch, optionally raises."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[UserIntervention] = []
        self._raise = raise_exc

    async def on_dispatch(self, iv: UserIntervention) -> None:
        self.calls.append(iv)
        if self._raise is not None:
            raise self._raise


class _Handler:
    """Records dispatch() calls; returns a sentinel answer."""

    def __init__(self) -> None:
        self.calls: list[UserIntervention] = []

    async def dispatch(self, iv: UserIntervention) -> InterventionAnswer:
        self.calls.append(iv)
        return InterventionAnswer(text="handled")


async def _noop_announce(iv: UserIntervention) -> None:
    return None


def _make_iv(*, run_id=None, origin_channel_id=None) -> UserIntervention:
    iv = UserIntervention(kind="ask_user", prompt="Q?", run_id=run_id)
    iv.origin_channel_id = origin_channel_id
    iv.future = asyncio.get_running_loop().create_future()
    return iv


def _coord(*, chain_map=None):
    registry = InterventionRegistry(on_announce=_noop_announce)
    handler = _Handler()
    events = _Events()
    coord = InterventionCoordinator(
        registry=registry,
        handler=handler,
        events=events,
        running_skills_chain_fn=lambda: (chain_map or {}),
    )
    return coord, registry, handler, events


# ── override accessors + is_override_active ─────────────────────────────────

@pytest.mark.asyncio
async def test_override_accessors_and_is_active() -> None:
    """Tier 2: register/unregister/has/get/count + is_override_active(run_id)."""
    coord, _registry, _handler, _events = _coord(chain_map={"r1": "c1"})
    bus = _Override()
    assert coord.override_count() == 0
    assert not coord.has_override("c1")
    assert not coord.is_override_active("r1")

    coord.register_override("c1", bus)
    assert coord.has_override("c1")
    assert coord.get_override("c1") is bus
    assert coord.override_count() == 1
    # run → chain → override
    assert coord.is_override_active("r1") is True
    assert coord.is_override_active("r2") is False  # r2 has no chain
    assert coord.is_override_active(None) is False

    coord.unregister_override("c1")
    assert not coord.has_override("c1")
    assert coord.override_count() == 0
    coord.unregister_override("c1")  # idempotent


# ── dispatch: override-observer side-effect (best-effort, before handler) ────

@pytest.mark.asyncio
async def test_dispatch_notifies_override_observer_then_handler() -> None:
    """Tier 2: a registered override's on_dispatch fires (side-effect) AND the
    iv still flows through the handler (override observes, does not replace)."""
    coord, registry, handler, _events = _coord(chain_map={"r1": "c1"})
    registry.register_listener("ch")  # present listener → no stall
    bus = _Override()
    coord.register_override("c1", bus)

    iv = _make_iv(run_id="r1", origin_channel_id="ch")
    answer = await coord.dispatch(iv)

    assert bus.calls == [iv]               # observer notified
    assert handler.calls == [iv]           # AND handler still reached
    assert answer.text == "handled"


@pytest.mark.asyncio
async def test_dispatch_override_observer_is_best_effort() -> None:
    """Tier 2: a raising on_dispatch must NOT block dispatch — the iv still
    reaches the handler."""
    coord, registry, handler, _events = _coord(chain_map={"r1": "c1"})
    registry.register_listener("ch")
    bus = _Override(raise_exc=RuntimeError("webhook down"))
    coord.register_override("c1", bus)

    iv = _make_iv(run_id="r1", origin_channel_id="ch")
    answer = await coord.dispatch(iv)

    assert bus.calls == [iv]
    assert handler.calls == [iv]           # reached despite the raise
    assert answer.text == "handled"


# ── dispatch: origin-pin stall (absent listener → parked) ───────────────────

@pytest.mark.asyncio
async def test_dispatch_parks_iv_when_origin_listener_absent() -> None:
    """Tier 2: an iv pinned to an origin_channel_id with no live listener is
    parked stalled (not delivered to the handler) and awaits its future."""
    coord, registry, handler, events = _coord()
    iv = _make_iv(origin_channel_id="gone")  # no listener "gone" registered

    task = asyncio.ensure_future(coord.dispatch(iv))
    await asyncio.sleep(0)  # let dispatch reach the park + await

    assert [iv2.id for iv2 in registry.list_stalled()] == [iv.id]  # parked
    assert handler.calls == []                                     # NOT handled
    assert events.emitted and events.emitted[0][0] == "intervention_routed"
    assert events.emitted[0][1]["route"] == "user_channel_stalled"

    # Resolve the parked future to clean up the awaiting task.
    iv.future.set_result(InterventionAnswer(text="claimed"))
    answer = await task
    assert answer.text == "claimed"


@pytest.mark.asyncio
async def test_dispatch_routes_to_handler_when_listener_present() -> None:
    """Tier 2: an iv whose origin listener IS present routes to the handler
    (no stall)."""
    coord, registry, handler, _events = _coord()
    registry.register_listener("ch")
    iv = _make_iv(origin_channel_id="ch")

    answer = await coord.dispatch(iv)

    assert handler.calls == [iv]
    assert registry.list_stalled() == []
    assert answer.text == "handled"
