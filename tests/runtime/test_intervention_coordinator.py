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


def _coord():
    registry = InterventionRegistry(on_announce=_noop_announce)
    handler = _Handler()
    events = _Events()
    coord = InterventionCoordinator(
        registry=registry,
        handler=handler,
        events=events,
    )
    return coord, registry, handler, events


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


# ── CancelledError propagation (#2414-I2 fix) ────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_cancel_propagates_not_silenced_stalled_path() -> None:
    """Tier 2: task cancellation propagates through the stalled-path dispatch.

    When an iv is parked stalled and the awaiting task is cancelled,
    CancelledError must propagate (task.cancelled() True) — swallowing it
    caused the skill to receive an empty answer and re-request the
    intervention, producing a teardown hang (#2414-I2)."""
    coord, _registry, _handler, _events = _coord()
    iv = _make_iv(origin_channel_id="gone")  # no listener → stalled path

    task = asyncio.ensure_future(coord.dispatch(iv))
    await asyncio.sleep(0)  # let dispatch reach park + await

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled(), "CancelledError must propagate — task must be cancelled, not complete silently"


@pytest.mark.asyncio
async def test_registry_dispatch_cancel_propagates_not_silenced() -> None:
    """Tier 2: task cancellation propagates through InterventionRegistry.dispatch.

    The registry's dispatch must re-raise CancelledError so the calling
    skill task is properly cancelled (#2414-I2). Before the fix, the
    except-swallow returned an empty answer; after, the task is cancelled."""
    registry = InterventionRegistry(on_announce=_noop_announce)
    iv = _make_iv()

    # Drive dispatch: it enqueues iv and blocks at await iv.future.
    task = asyncio.ensure_future(registry.dispatch(iv))
    await asyncio.sleep(0)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled(), "CancelledError must propagate — task must be cancelled, not silently return empty"
