"""Tier 2: #5977 ①③ — the stall-dump self-amplification fix.

``watch_event_loop`` re-armed ``StallDumpArm`` unconditionally every tick,
with zero coupling to ``LoopTripwire``'s own episode-level state — a
long-perceived-as-one stall episode is actually many separate tick-level
arm→(maybe fire)→rearm cycles, each of which can independently dump the
full thread stack, and each dump's own synchronous write cost adds to the
very lateness that risks triggering the next one (owner-hit, the log
"ひたすら繰り返されてる"). ① caps this at one dump per episode plus a
session-total backstop (:data:`~reyn.runtime.loop_tripwire._SESSION_DUMP_CAP`);
③ makes reaching that cap always-visible, never silent (the same
"unmeasured ≠ 0" shape #5959 named).

The clock is an INPUT here too (CLAUDE.md: no duration in a test, in either
direction) — reuses ``test_5898_loop_tripwire.py``'s own ``_ScriptedClock``
shape rather than a second copy.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from reyn.runtime.loop_tripwire import LoopTripwire, StallDumpArm, watch_event_loop


class _ScriptedClock:
    """See ``test_5898_loop_tripwire.py``'s own copy for the full
    rationale — duplicated rather than imported across test files per this
    repo's own test-isolation convention (no shared test-only helper
    module for a two-file, two-line class)."""

    def __init__(self, instants: "list[float]") -> None:
        self._instants = list(instants)

    def __call__(self) -> float:
        if not self._instants:
            raise asyncio.CancelledError
        return self._instants.pop(0)

    async def sleep(self, _seconds: float) -> None:
        if not self._instants:
            raise asyncio.CancelledError


def _open_arm(tmp_path: Path, *, label: str) -> StallDumpArm:
    path = tmp_path / "reyn.log"
    path.write_text("", encoding="utf-8")
    arm = StallDumpArm.open(seconds=0.25, log_path=str(path), logger=logging.getLogger(label), label=label)
    assert arm is not None
    return arm


@pytest.mark.asyncio
async def test_watch_event_loop_dumps_at_most_once_per_stall_episode(tmp_path: Path) -> None:
    """Tier 2: #5977 ①. Clock script: healthy(0.05) → late +350ms(0.45, onset) → late
    +350ms(0.85, the SAME still-ongoing episode — no recovery in between)
    → recovered(0.90). TWO over-threshold ticks, one episode.

    Without ①'s gate, ``StallDumpArm.rearm()`` runs every tick
    unconditionally, so BOTH over-threshold ticks would read back
    ``stack_dumped=True`` — the issue's own reported pattern (many
    consecutive ``Thread 0x`` blocks for one stall). With it, the SECOND
    over-threshold tick's re-arm is skipped entirely
    (``should_arm_stack_dump()`` is ``False`` once the episode already
    dumped — there is no separate ``faulthandler`` state to cancel;
    skipping the re-arm IS the suppression), so
    ``LoopTripwire.session_dump_count`` stays at 1 for the whole episode.

    Strip-falsify (verified by hand: `should_arm_stack_dump` temporarily
    forced to always return ``True`` around the ``rearm()`` call in
    ``watch_event_loop``): ``session_dump_count`` becomes 2 for this exact
    script — the issue's own accept criterion ("抑制を外すと、同じ stall
    で複数セット出ることを確認")."""
    arm = _open_arm(tmp_path, label="t1")
    rearm_calls = 0
    real_rearm = arm.rearm

    def counting_rearm() -> bool:
        nonlocal rearm_calls
        rearm_calls += 1
        return real_rearm()

    arm.rearm = counting_rearm  # type: ignore[method-assign]

    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.85, 0.90])
    tripwire = LoopTripwire(threshold_ms=250.0)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
    finally:
        arm.close()

    assert tripwire.session_dump_count == 1, "one over-threshold tick already dumped; the second must not"
    assert rearm_calls == 3, (
        "initial arm + the ONE tick that dumped — the second over-threshold "
        "tick's re-arm must be skipped, not just its readback ignored"
    )


@pytest.mark.asyncio
async def test_a_second_stall_episode_gets_its_own_fresh_dump_allowance(tmp_path: Path) -> None:
    """Tier 2: #5977 ①. Recovery must reset the per-episode gate (not the session count): a
    SECOND, separate stall episode after a full recovery gets its own
    dump. Script: healthy → late(onset A, dump#1) → recovered(A) →
    late(onset B, dump#2) → recovered(B)."""
    arm = _open_arm(tmp_path, label="t2")
    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.50, 0.90, 0.95])
    tripwire = LoopTripwire(threshold_ms=250.0)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
    finally:
        arm.close()

    assert tripwire.session_dump_count == 2, "two SEPARATE episodes each get their own one-shot dump"


@pytest.mark.asyncio
async def test_session_dump_cap_notice_fires_exactly_once(tmp_path: Path) -> None:
    """Tier 2: #5977 ③. Two separate episodes (A, B), ``session_dump_cap=2`` — B's
    dump reaches the cap, firing ``on_dump_cap_reached`` once with
    ``(2, 2)``. A THIRD episode (C) still reports its OWN stall notice
    (dump suppression and the stall notice are independent signals — see
    ``LoopTripwire.observe`` vs. ``record_stack_dump``) but produces no
    further dump and no further cap notice: the count stays at 2 and the
    callback fires only once total, never silently past the cap.

    Strip-falsify (verified by hand: dropping the ``not self.
    _cap_notice_emitted`` guard in ``record_stack_dump``): the callback
    would fire AGAIN in episode C too, since the count is still ``>=
    cap`` there — this test's call-count-of-exactly-one assertion catches
    that."""
    arm = _open_arm(tmp_path, label="t3")
    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.85, 0.90, 1.30, 1.35, 1.75, 1.80])
    tripwire = LoopTripwire(threshold_ms=250.0, session_dump_cap=2)
    stalls: "list[float]" = []
    cap_reached: "list[tuple[int, int]]" = []
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=stalls.append,
                stack_dump=arm,
                on_dump_cap_reached=lambda n, cap: cap_reached.append((n, cap)),
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
    finally:
        arm.close()

    assert stalls == [pytest.approx(350.0)] * 3, "three separate episodes each still report their own onset"
    assert tripwire.session_dump_count == 2, "episode C's stall must not produce a third dump"
    assert cap_reached == [(2, 2)], "the cap-reached notice fires exactly once, on the tick it is reached"


def test_stall_dump_cap_reached_log_line_names_the_count_and_cap() -> None:
    """Tier 1: the ③ notice text — a structural contract check, not a
    duration or algorithm-level pin. Strip-falsify: dropping either
    f-string operand from the free function's return would fail one of
    these two ``in`` checks."""
    from reyn.runtime.loop_tripwire import stall_dump_cap_reached_log_line

    line = stall_dump_cap_reached_log_line(10, 10)
    assert "10/10" in line
    assert "no further stack dumps" in line
