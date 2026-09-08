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
import inspect
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

    Strip-falsify (verified by hand: the ``tripwire.should_arm_stack_dump()``
    read in the post-``observe()`` re-arm decision temporarily forced to
    ``True``): ``session_dump_count`` becomes 2 for this exact script —
    the issue's own accept criterion ("抑制を外すと、同じ stall で複数セット
    出ることを確認").

    ⚠️ This assertion alone does NOT witness the SEPARATE ordering defect
    lead-coder found in review (PR #5980 BLOCKING): whether re-arming is
    decided BEFORE vs. AFTER this tick's own detection can leave a REAL,
    uncancelled ``faulthandler`` timer pending even while
    ``session_dump_count`` stays byte-identical (bookkeeping, not "how
    many actually landed on disk") — a real second dump needs real
    wall-clock time to fire, which a scripted-clock test structurally
    cannot wait for (CLAUDE.md: no duration a test depends on). See
    ``test_watch_event_loop_decides_rearm_after_observing_not_before``
    below for that property's own (structural) witness."""
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

    Strip-falsify (verified by hand: ``record_stack_dump``'s ``return
    self._session_dump_count == self._session_dump_cap`` changed to
    ``return True``): the callback fires on EVERY dump instead of only
    the one that reaches the cap — ``cap_reached`` becomes
    ``[(1, 2), (2, 2)]`` for this exact script instead of ``[(2, 2)]``,
    which this test's equality assertion catches."""
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


def test_watch_event_loop_decides_rearm_after_observing_not_before() -> None:
    """Tier 1: wiring gate — #5977's actual reported bug (lead-coder
    BLOCKING, PR #5980) cannot be witnessed by ANY scripted-clock unit
    test: the dangerous extra re-arm is a REAL ``faulthandler`` one-shot
    timer that only fires after REAL wall-clock time elapses, and
    CLAUDE.md's testing policy forbids a test that depends on waiting one
    out. The only CI-safe witness left is the SOURCE ORDER itself — the
    same shape ``test_5898_loop_tripwire.py``'s own
    ``test_the_fail_close_driver_passes_its_own_lateness_to_the_sweep``
    uses for an equivalent timer-driven ordering.

    ``LoopTripwire.should_arm_stack_dump()``'s answer is only correct for
    THIS tick once ``tripwire.observe()`` has already run — ``observe()``
    is what resets the per-episode dump allowance at a recovery
    transition and what closes it via ``record_stack_dump()`` on a fire.
    Deciding earlier reads a stale flag on exactly the tick that matters
    (see the previous test's own docstring) — so the re-arm decision must
    appear AFTER ``observe()`` in source. Strip-falsify: reordering the
    two lines in ``watch_event_loop`` would put ``observe_pos`` after
    ``rearm_decision_pos``, failing this ``>``."""
    source = inspect.getsource(watch_event_loop)
    observe_pos = source.index("tripwire.observe(")
    rearm_decision_pos = source.index("armed_last_tick = bool(")
    assert rearm_decision_pos > observe_pos, (
        "the re-arm decision must read should_arm_stack_dump() AFTER observe() "
        "has already run this tick, not before"
    )


def test_app_inline_tick_loop_decides_rearm_after_observing_not_before() -> None:
    """Tier 1: the SAME wiring gate as
    ``test_watch_event_loop_decides_rearm_after_observing_not_before``,
    against ``TextualChatApp._watch_loop_responsiveness``'s own inline
    copy of the tick loop (it does not call ``watch_event_loop`` — a
    pre-existing duplication, out of #5977's scope to unify — so the
    same ordering fix was applied twice and needs its own witness)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    source = inspect.getsource(TextualChatApp._watch_loop_responsiveness)
    observe_pos = source.index("self._loop_tripwire.observe(")
    rearm_decision_pos = source.index("_armed_last_tick = bool(")
    assert rearm_decision_pos > observe_pos, (
        "the re-arm decision must read should_arm_stack_dump() AFTER observe() "
        "has already run this tick, not before"
    )


def test_stall_dump_cap_reached_log_line_names_the_count_and_cap() -> None:
    """Tier 1: the ③ notice text — a structural contract check, not a
    duration or algorithm-level pin. Strip-falsify: dropping either
    f-string operand from the free function's return would fail one of
    these two ``in`` checks."""
    from reyn.runtime.loop_tripwire import stall_dump_cap_reached_log_line

    line = stall_dump_cap_reached_log_line(10, 10)
    assert "10/10" in line
    assert "no further stack dumps" in line
