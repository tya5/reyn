"""Tier 2: #5898 — the runtime-generic loop tripwire (lifted out of the
inline CUI so ``reyn:web`` can arm it) and the heartbeat sweeper's
server-stall credit.

The clock is an INPUT everywhere here (CLAUDE.md: a duration is never a
wait): ``watch_event_loop`` takes ``clock``/``sleep`` so a stall is a jump
in the supplied clock, and ``SurfaceManager.sweep_dead`` / ``sweeper_stall_
credit`` are pure in ``now``. Nothing sleeps on the loop it measures.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import pytest

from reyn.interfaces.transport.agui.endpoint import _drive_fail_close, sweeper_stall_credit
from reyn.interfaces.transport.agui.surface import SurfaceManager
from reyn.runtime.loop_tripwire import LoopTripwire, StallDumpArm, watch_event_loop


class _ScriptedClock:
    """A clock that returns the next scripted instant per read; ``sleep``
    is a no-op await that ends the watched loop (``CancelledError``, the
    loop's own shutdown path) once the script is exhausted."""

    def __init__(self, instants: "list[float]") -> None:
        self._instants = list(instants)
        self.reads = 0

    def __call__(self) -> float:
        if not self._instants:
            raise asyncio.CancelledError
        self.reads += 1
        return self._instants.pop(0)

    async def sleep(self, _seconds: float) -> None:
        if not self._instants:
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_watch_event_loop_reports_each_stall_episode_once_and_its_recovery_once() -> None:
    """Tier 2: a tick that lands 350 ms late (threshold 250) fires
    ``on_stall`` ONCE with that magnitude; the next on-time tick fires
    ``on_recovered`` ONCE; a further on-time tick fires nothing. Clock
    instants: start 0.0 → ticks at 0.05 (on time), 0.45 (late by 0.35),
    0.50 (on time), 0.55 (on time). Strip-falsify: drop the
    ``on_recovered`` branch → the recovery list stays empty → red; report
    every late tick instead of once per episode → a second scripted late
    tick would double the stall list → red (see the second test)."""
    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.50, 0.55])
    stalls: list[float] = []
    recovered: list[bool] = []
    tripwire = LoopTripwire(threshold_ms=250.0)

    with pytest.raises(asyncio.CancelledError):
        await watch_event_loop(
            tripwire,
            on_stall=stalls.append,
            on_recovered=lambda: recovered.append(True),
            tick_seconds=0.05,
            clock=clock,
            sleep=clock.sleep,
        )

    assert stalls == [pytest.approx(350.0)], "one notice, carrying the episode's magnitude"
    assert recovered == [True]
    assert tripwire.max_lateness_ms == pytest.approx(350.0)


@pytest.mark.asyncio
async def test_watch_event_loop_does_not_repeat_a_still_ongoing_stall() -> None:
    """Tier 2: two consecutive late ticks are ONE episode — one notice, not
    two (a notice per tick buries the reply it is about, #3539)."""
    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.85, 0.90])
    stalls: list[float] = []
    recovered: list[bool] = []

    with pytest.raises(asyncio.CancelledError):
        await watch_event_loop(
            LoopTripwire(threshold_ms=250.0),
            on_stall=stalls.append,
            on_recovered=lambda: recovered.append(True),
            tick_seconds=0.05,
            clock=clock,
            sleep=clock.sleep,
        )

    assert stalls == [pytest.approx(350.0)], "the second late tick is the SAME episode — no second notice"
    assert recovered == [True]


def test_stall_dump_arm_needs_a_path_and_arms_against_its_own_fd(tmp_path: Path) -> None:
    """Tier 2: no path → no arm at all (#5877: a dump with no stable
    destination is never attempted). With one: ``rearm`` arms against the
    arm's own fd, and stays armed across repeated calls.

    #5977 ②: the dump's OWN dedicated file is never externally rotated
    (unlike the pre-②  design, which dumped into ``reyn.log`` and had to
    survive ``RotatingFileHandler``'s own rollover — see
    ``tests/runtime/test_5977_stall_dump_suppression.py`` for the
    truncate-for-the-next-episode behaviour that replaced that rotation
    handling)."""
    log = logging.getLogger("test_5898_stall_dump_arm")
    assert StallDumpArm.open(seconds=0.25, path=None, logger=log, label="t") is None

    path = tmp_path / "stall_dump.log"
    arm = StallDumpArm.open(seconds=60.0, path=str(path), logger=log, label="t")
    assert arm is not None
    try:
        assert arm.rearm() is True
        assert arm.rearm() is True, "repeated re-arms against the same fd must keep succeeding"
    finally:
        arm.close()
    assert arm.armed is False


def _manager() -> SurfaceManager:
    return SurfaceManager(authorized=lambda uid: bool(uid), liveness_timeout=45.0)


def test_a_server_stall_is_credited_to_the_client_not_counted_as_its_silence() -> None:
    """Tier 2: #5898's named second-order effect (architect: "server 自身の
    stall が liveness_timeout を超えると自分の client を heartbeat-timeout で
    detach し得る") — a heartbeat is stamped at the server's RECEIVE time,
    so after a 120 s server stall the sweeper's first tick sees a
    121 s-old heartbeat. With the stall credited, the client stays
    attached; with no credit (the pre-#5898 reading) it is detached.
    Strip-falsify: ignore ``stall_credit_s`` in ``sweep_dead`` → red."""
    m = _manager()
    m.attach("c1", "u", now=0.0)
    m.heartbeat("c1", now=10.0)

    now = 10.0 + 121.0
    assert m.sweep_dead(now, stall_credit_s=0.0) == ["c1"], "control: without credit, it IS swept"

    m2 = _manager()
    m2.attach("c1", "u", now=0.0)
    m2.heartbeat("c1", now=10.0)
    assert m2.sweep_dead(now, stall_credit_s=120.0) == []
    assert m2.sweep_dead(now + 45.0 + 1.0, stall_credit_s=0.0) == ["c1"], (
        "the credit only delays a genuine detach by the stall's length — the next "
        "on-time tick judges the full interval again"
    )


def test_sweeper_stall_credit_is_the_ticks_lateness_beyond_its_period() -> None:
    """Tier 2: an on-time tick credits nothing (a healthy loop is
    byte-identical to before); a tick that ran 120 s after its period
    credits exactly that."""
    assert sweeper_stall_credit(now=10.5, last_tick=10.0, poll=0.5) == 0.0
    assert sweeper_stall_credit(now=10.4, last_tick=10.0, poll=0.5) == 0.0
    assert sweeper_stall_credit(now=130.5, last_tick=10.0, poll=0.5) == pytest.approx(120.0)


def test_the_fail_close_driver_passes_its_own_lateness_to_the_sweep() -> None:
    """Tier 2: wiring gate — ``_drive_fail_close`` is timer-driven
    (``asyncio.sleep(poll)`` + ``monotonic()``), so the ONLY way to
    witness that it hands its lateness to ``sweep_dead`` without a real
    stall (a duration) is the source itself — the same
    ``inspect.getsource`` shape ``test_5364_media_store_flush_barrier.py``
    uses for ``run_loop``'s barrier. A reference that breaks on the
    change it cares about: renaming or dropping the credit argument
    fails this line."""
    source = inspect.getsource(_drive_fail_close)
    assert "stall_credit_s=sweeper_stall_credit(now=now, last_tick=last_tick, poll=poll)" in source
    assert "last_tick = now" in source
