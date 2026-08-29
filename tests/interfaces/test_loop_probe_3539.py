"""Tier 2: the loop instrumentation is quiet by default and useful when it is not.

#3539 stalled twice for reasons this module answers. The symptom ("the UI froze
during a stream") arrives unannounced, so an opt-in probe is always enabled one
occurrence too late — #3638 closed exactly that way. And when numbers were
finally taken, they could not be compared to the owner's environment, because
nobody had recorded which axes differed.

So: a tripwire that is always on and costs a float comparison, and detail behind
``REYN_PROF_DUMP``. These pin both halves — that the default path writes nothing
and touches no file, and that the tripwire speaks exactly once, with a magnitude
and a next step.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.widgets import Tab
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp, loop_probe
from reyn.interfaces.inline.textual_chat.chrome import StatusLine
from reyn.interfaces.inline.textual_chat.loop_probe import (
    LoopTripwire,
    dump_path,
    environment_axes,
    stall_banner,
    stall_log_line,
    stall_recovered_log_line,
    write_record,
)
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (the idiom shared with ``tests/interfaces/test_3288_3c_tui_delta_coalesce.py``)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _painted(app: TextualChatApp) -> str:
    """Everything the compositor put on screen — the surface the operator
    actually reads, and the only one that shows an overlay as delivered."""
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    )


class _VirtualClock:
    """#4844 (owner: "また時間依存テスト作ってるの？"): a ``time.perf_counter``
    stand-in that behaves exactly like the real clock until told to
    :meth:`jump` forward.

    ``TextualChatApp._watch_loop_responsiveness`` computes lateness as
    ``time.perf_counter() - last - _TICK_SECONDS`` every tick — a REAL
    ``time.sleep(stall_seconds)`` was the only thing making that lateness
    exceed the tripwire's threshold, with a 150ms margin meant to
    guarantee it. That margin is real-wall-clock-relative, so it depends on
    CI-host scheduling being fast enough to keep the *actual* delay within
    150ms of the intended one — CLAUDE.md's banned "straight-line sleep(N)
    as the thing that makes an assertion pass", and #4834 measured real
    CI-host starvation at ~2s, an order of magnitude past that margin
    (tracked separately in #4827 as "why does the starvation happen" —
    this fixes "why does the test's OWN pass/fail depend on wall-clock
    time at all").

    A monkeypatched ``time.perf_counter`` that returns a FIXED large jump
    unconditionally would break every *other* tick this same long-lived
    background loop takes (it starts at ``on_mount``, runs the whole test)
    — so this tracks a virtual offset, added to the REAL clock: every tick
    before/after :meth:`jump` behaves physically (tiny real deltas, exactly
    like an un-patched clock), and exactly one intended tick sees the
    injected jump — deterministic, and zero real elapsed wall-clock time.

    Patches ``time.perf_counter`` specifically (not ``time.monotonic``,
    which is what ``asyncio``'s own scheduling and ``TextualChatApp``'s
    ``self._clock``/``ActivityRow`` elapsed-time default to — confirmed via
    ``app.py``'s own ``clock: Callable[[], float] = time.monotonic``), so
    this is isolated to exactly the one call site it targets.
    """

    def __init__(self) -> None:
        # Captured NOW, before any monkeypatch.setattr(time, "perf_counter",
        # self) call — must be the REAL underlying function, not a dynamic
        # `time.perf_counter` lookup inside __call__, which would resolve to
        # THIS INSTANCE once patched in (infinite recursion).
        self._real_perf_counter = time.perf_counter
        self._offset = 0.0

    def __call__(self) -> float:
        return self._real_perf_counter() + self._offset

    def jump(self, seconds: float) -> None:
        """Advance the virtual clock by ``seconds``, with no real delay —
        the NEXT tick's ``time.perf_counter()`` reads this far ahead of the
        previous one."""
        self._offset += seconds


def test_detail_is_off_unless_a_path_is_named(monkeypatch) -> None:
    """Tier 2: no env var, no destination — the detail layer is inert.

    Asserted on ``dump_path`` rather than on the absence of a file, because a
    probe that computed a record and then discarded it would pass a
    file-absence check while still paying for the record on every delta.
    """
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)

    assert dump_path() is None


def test_write_record_touches_nothing_when_off(monkeypatch, tmp_path: Path) -> None:
    """Tier 2: the default path creates no file anywhere.

    The directory is checked before and after so this fails on a stray write to
    a default location, not only on a write to the path a test named.
    """
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())

    write_record("chunk", wait_ms=1.0, work_ms=0.3)

    assert set(tmp_path.iterdir()) == before


def test_write_record_writes_when_a_path_is_named(monkeypatch, tmp_path: Path) -> None:
    """Tier 2: switched on, a record lands and carries its environment.

    The axes are asserted as present rather than by value — which platform is
    running the suite is not this test's business, but that a later capture can
    be compared to an earlier one is.
    """
    import json

    target = tmp_path / "probe.jsonl"
    monkeypatch.setenv("REYN_PROF_DUMP", str(target))

    write_record("chunk", wait_ms=17.8, work_ms=0.31)

    record = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert record["kind"] == "chunk"
    assert record["wait_ms"] == 17.8
    assert record["env"], "a record with no environment axes cannot be compared later"


def test_the_environment_axes_name_what_differed() -> None:
    """Tier 2: the axes #3539 needed are the ones collected.

    #3539 could not be settled because the owner's environment and the
    measuring one differed along axes nobody had written down. Platform and
    flowview version are the two available without a live session; the
    per-record fields (model, provider) are supplied by the caller.
    """
    axes = environment_axes()

    assert "platform" in axes
    assert "python" in axes


def test_the_tripwire_stays_quiet_on_a_healthy_loop() -> None:
    """Tier 2: a healthy stream never trips it.

    The measured baseline is a 10 ms-period task never exceeding 12 ms over 463
    chunks. A tripwire that fired on those would be read as noise and ignored,
    which is the same as not having one.
    """
    tripwire = LoopTripwire()

    assert all(tripwire.observe(lateness) is None for lateness in (0.1, 5.0, 12.0, 40.0))
    assert not tripwire.fired


def test_it_speaks_once_with_a_magnitude_and_a_next_step() -> None:
    """Tier 2: the first crossing says how bad, and what to do about it.

    Once, deliberately: a freeze is one event to the person watching it, and a
    notice repeated per tick would bury the reply it is about. The message is
    checked for the magnitude and the env var because a bare "something was
    slow" leaves the reader exactly where #3539 already was.
    """
    tripwire = LoopTripwire(threshold_ms=250.0)

    first = tripwire.observe(1800.0)
    second = tripwire.observe(2400.0)

    assert first is not None
    assert second is None, "a freeze is one event, not one per tick"
    assert "1.8s" in stall_banner(first), (
        "the status-line segment must carry the magnitude — a bare 'something "
        "was slow' leaves the reader where #3539 already was"
    )
    assert "1.8s" in stall_log_line(first)
    assert "REYN_PROF_DUMP" in stall_log_line(first), (
        "the durable record must say how to capture the detail next time"
    )


def test_the_durable_record_keeps_landing_while_the_banner_stays_quiet(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: #4761 ① — one ``_fired`` flag used to gate BOTH the once-only
    human notice AND the durable ``write_record`` call, so a stall lasting
    past the first tick left no durable trace of whether it recovered or
    kept getting worse — exactly the question a frozen screen cannot answer
    on its own. The once-only rule stays for the notice (unchanged by this
    fix — see ``test_it_speaks_once_with_a_magnitude_and_a_next_step``
    above); the durable record must keep landing independently, at
    :data:`~reyn.interfaces.inline.textual_chat.loop_probe._RECORD_INTERVAL_S`
    granularity, for as long as the stall continues.
    """
    import json

    target = tmp_path / "probe.jsonl"
    monkeypatch.setenv("REYN_PROF_DUMP", str(target))

    clock = [1000.0]
    monkeypatch.setattr(loop_probe.time, "monotonic", lambda: clock[0])

    tripwire = LoopTripwire(threshold_ms=250.0)

    first = tripwire.observe(1800.0)  # crosses — records AND fires the notice
    clock[0] += 0.5  # inside the interval — still stalled, must NOT record yet
    still_quiet = tripwire.observe(1900.0)
    clock[0] += loop_probe._RECORD_INTERVAL_S  # interval elapsed, still stalled
    still_stalled = tripwire.observe(2000.0)
    clock[0] += loop_probe._RECORD_INTERVAL_S
    recovered = tripwire.observe(50.0)  # back under threshold — no record

    assert first is not None, "the first crossing must still fire the notice"
    assert still_quiet is None, "the once-only notice must not repeat mid-interval"
    assert still_stalled is None, (
        "the notice stays quiet after the first crossing regardless of the "
        "record interval — this fix only changes the durable record's cadence"
    )
    assert recovered is None

    records = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    lateness_values = [r["lateness_ms"] for r in records if r["kind"] == "tripwire"]
    assert lateness_values == [1800.0, 2000.0], (
        "expected exactly the first crossing and the one past the interval — "
        f"the mid-interval tick and the recovered tick must not add entries: {records!r}"
    )
    recovered_lateness = [
        r["lateness_ms"] for r in records if r["kind"] == "tripwire_recovered"
    ]
    assert recovered_lateness == [50.0], (
        "the recovered tick must write exactly ONE tripwire_recovered record — "
        "without it, a trace that just stops leaves 'it recovered' and 'the "
        f"process died mid-stall' looking identical: {records!r}"
    )


def test_recovery_is_recorded_only_once_per_episode(monkeypatch, tmp_path: Path) -> None:
    """Tier 2: #4761 ① follow-up — a SECOND healthy tick after recovery must
    not write a second ``tripwire_recovered`` record (it was already healthy,
    nothing changed), and a SECOND stall episode must be able to record its
    own recovery independently of the first."""
    import json

    target = tmp_path / "probe.jsonl"
    monkeypatch.setenv("REYN_PROF_DUMP", str(target))

    clock = [2000.0]
    monkeypatch.setattr(loop_probe.time, "monotonic", lambda: clock[0])

    tripwire = LoopTripwire(threshold_ms=250.0)

    tripwire.observe(1800.0)  # episode 1: stall
    clock[0] += 1.0
    tripwire.observe(50.0)  # episode 1: recovers
    clock[0] += 1.0
    tripwire.observe(60.0)  # still healthy — no NEW recovery
    clock[0] += 1.0
    tripwire.observe(1700.0)  # episode 2: stall again
    clock[0] += 1.0
    tripwire.observe(40.0)  # episode 2: recovers

    records = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    recovered_lateness = [
        r["lateness_ms"] for r in records if r["kind"] == "tripwire_recovered"
    ]
    assert recovered_lateness == [50.0, 40.0], (
        f"expected exactly one recovery record per episode: {records!r}"
    )


def test_a_second_stall_after_recovery_is_reported_again() -> None:
    """Tier 2: #4855 (owner decision B) — the once-only notice gate is now
    "once per un-recovered episode," not "once per App session." Before
    this fix, ``self._fired`` was a PERMANENT one-shot latch: a first
    stall (even an unrelated app-mount startup hiccup) consumed the
    session's only notice, and every LATER, possibly far more serious
    freeze went unreported for the rest of the session — #4855's own
    measured defect. ``observe()``'s recovery branch now resets
    ``_fired`` at the exact point a stall recovers, so a genuinely NEW
    episode after that point gets its own onset reported, exactly like
    the first ever did.

    This is the witness lead-coder asked for directly: "#4827①'s test
    assumed 'my own stall produces a notice' — confirm that assumption
    is TRUE AGAIN once B lands," not just that #4855's old symptom is
    gone."""
    tripwire = LoopTripwire(threshold_ms=250.0)

    first_onset = tripwire.observe(1800.0)  # episode 1: stall
    tripwire.observe(50.0)  # episode 1: recovers
    assert tripwire.consume_recovered() is True, (
        "episode 1's own onset was reported, so its recovery must be too"
    )

    second_onset = tripwire.observe(1700.0)  # episode 2: a FRESH stall, post-recovery
    tripwire.observe(40.0)  # episode 2: recovers

    assert first_onset is not None
    assert second_onset is not None, (
        "episode 2 comes AFTER episode 1's own recovery -- its onset must "
        "be reported, the same as episode 1's was (owner decision B: "
        "'once per session' was the defect this issue closes)"
    )
    assert tripwire.consume_recovered() is True, (
        "episode 2's own onset was reported, so its recovery must be too"
    )


def test_a_second_tick_within_the_same_still_ongoing_episode_is_not_reported_again() -> None:
    """Tier 2: the ORIGINAL reason for "once, not per-tick" still holds
    after #4855's fix — a stall that has not yet recovered must not
    repeat its notice on every tick (a notice repeated per tick buries
    the reply it's about). Only RECOVERY resets the gate; a later tick
    that is STILL above threshold, with no recovery in between, must stay
    quiet, same as before this fix."""
    tripwire = LoopTripwire(threshold_ms=250.0)

    onset = tripwire.observe(1800.0)  # crosses — reports once
    still_stalled = tripwire.observe(1900.0)  # same episode, no recovery yet
    still_stalled_again = tripwire.observe(2000.0)  # same episode, still no recovery

    assert onset is not None
    assert still_stalled is None, "the same still-ongoing episode must not re-report"
    assert still_stalled_again is None, "the same still-ongoing episode must not re-report"


def test_consume_recovered_needs_no_dump_env_at_all(monkeypatch) -> None:
    """Tier 2: #4797 follow-up (architect finding) — ``consume_recovered()``
    is a plain in-memory flag, reachable with ``REYN_PROF_DUMP`` unset the
    whole time. Every OTHER new signal added for #4761 ① (the repeated
    ``"tripwire"`` record, ``"tripwire_recovered"``) goes through
    :func:`write_record`, a no-op on the shipped default — a session that
    never armed the dump got nothing new from that work, the exact
    "visible with the shipped config?" gap CLAUDE.md names. This one does
    not depend on the file at all.
    """
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    tripwire = LoopTripwire(threshold_ms=250.0)

    assert tripwire.consume_recovered() is False, "nothing has stalled yet"
    tripwire.observe(1800.0)  # stall
    assert tripwire.consume_recovered() is False, "still stalled — not recovered"
    tripwire.observe(50.0)  # recovers
    assert tripwire.consume_recovered() is True
    assert tripwire.consume_recovered() is False, (
        "consuming it must clear the flag — a second read must not repeat it"
    )
    assert "recovered" in stall_recovered_log_line()


@pytest.mark.asyncio
async def test_recovery_notice_is_visible_without_arming_the_dump(caplog, monkeypatch) -> None:
    """Tier 2b: #4797 follow-up (architect finding), REACHED from the running
    app with REYN_PROF_DUMP deliberately left UNSET — the exact "unarmed
    session" situation #4761's own report was in. The stall notice already
    proved reachable in ``test_the_app_actually_shows_the_notice_when_the_
    loop_stalls`` above; this proves the RECOVERY notice is too, without
    which "it fired, then went quiet" was indistinguishable from "it fired,
    then the process died" on a session nobody armed in advance.
    """
    import logging

    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    logger_name = "reyn.interfaces.inline.textual_chat.app"
    # #4844: a virtual clock, not a real time.sleep() — see _VirtualClock's
    # own docstring for why a real sleep + margin is banned (CLAUDE.md) and
    # CI-unsafe (#4834: real starvation measured at ~2s, an order of
    # magnitude past any margin a test could afford to wait for).
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    stall_seconds = (loop_probe._TRIPWIRE_MS + 150) / 1000
    with caplog.at_level(logging.WARNING, logger=logger_name):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # #4855 follow-up (lead-coder's TESTS-READ, two rounds on this
            # same PR): the App's own, session-lifetime LoopTripwire —
            # an unrelated real stall during mount (real CI-host
            # contention, before this line ever runs) can already have
            # consumed its one-shot _fired latch. Post-fix, THIS test's
            # own jump below would then have its onset silently
            # swallowed, so its recovery is correctly NOT reported — and
            # the "recovered" wait below hangs until CI's own
            # --timeout=120 kills it, not a fast, readable assert.
            # app.reset_loop_tripwire() (public — round 2 of review:
            # directly assigning app._loop_tripwire from a test is a
            # private WRITE a future rename would silently stop
            # protecting, worse than a private read) gives this test's
            # own stall/recovery pair a clean instance, deterministic
            # regardless of what happened during mount.
            app.reset_loop_tripwire()
            # No real delay: the tripwire's own tick reads this jump on its
            # next wake, exactly as if the loop had actually gone
            # unresponsive for stall_seconds — the ticks afterward are
            # healthy again (no further jump), which is what the recovery
            # notice fires on.
            clock.jump(stall_seconds)
            # No test-owned wait budget (CLAUDE.md/testing.md § Time): wait
            # for the actual condition, unbounded — CI's own --timeout=120
            # is the kill switch if the wiring is ever broken and this never
            # arrives.
            while "recovered" not in caplog.text:
                await pilot.pause()

    assert "unresponsive" in caplog.text, "the stall itself must still be reported"
    assert "recovered" in caplog.text, (
        "the recovery notice must be readable with REYN_PROF_DUMP unset — "
        f"the exact situation #4761's own report was in: {caplog.text!r}"
    )


@pytest.mark.asyncio
async def test_recovery_notice_survives_the_real_interactive_logging_floor(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: #4801 follow-up (self-caught, measured, then corrected) —
    REACHED from the running app under the REAL production logging floor,
    not a hand-written log call standing in for it.

    The interactive CUI's own startup (``interfaces/cli/commands/chat.py``'s
    ``_setup_interactive_logging``) calls ``logging.basicConfig(level=
    logging.WARNING, force=True, ...)``, setting the ROOT logger's level.
    ``caplog.at_level(...)`` — what the sibling test above uses — overrides
    exactly that floor FOR THE TEST, which is why an earlier version of
    this PR's own ``logger.info`` recovery notice passed its own test while
    being silently dropped in the real interactive path (measured
    directly). The fix was raising the call site's severity to match the
    stall notice's ``logger.warning``, not raising this logger's level —
    so this test drives the SAME ``_watch_loop_responsiveness`` code path
    the sibling test does, but reads the notice back from a real log FILE
    under a reconstructed real floor, with no ``caplog`` involved at any
    point. A test that called ``logger.warning`` directly here would only
    prove the assertion, not that app.py's own call site still does.
    """
    import logging

    logfile = tmp_path / "reyn.log"
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    try:
        logging.basicConfig(
            filename=str(logfile), level=logging.WARNING, force=True,
        )
        stall_seconds = (loop_probe._TRIPWIRE_MS + 150) / 1000
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # #4855 follow-up — see the sibling test above's identical
            # comment: a fresh LoopTripwire makes this test's own
            # stall/recovery pair deterministic regardless of a real,
            # unrelated stall during mount having already consumed the
            # App's session-lifetime one-shot notice.
            app.reset_loop_tripwire()
            clock.jump(stall_seconds)
            # No test-owned wait budget: wait on the file's actual content,
            # unbounded — CI's own --timeout=120 is the kill switch.
            while "recovered" not in logfile.read_text(encoding="utf-8"):
                await pilot.pause()
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    content = logfile.read_text(encoding="utf-8")
    assert "unresponsive" in content, "the stall notice must reach the file under the real floor"
    assert "recovered" in content, (
        "the recovery notice must reach the file under the real floor too — "
        f"both are logger.warning, the same severity: {content!r}"
    )


def test_the_worst_lateness_survives_the_tick_that_saw_it() -> None:
    """Tier 2: the magnitude is retained, not just the fact.

    "It stalled" with no number cannot be compared to another run — which is
    the failure this module exists to stop repeating.
    """
    tripwire = LoopTripwire()

    for lateness in (10.0, 900.0, 30.0):
        tripwire.observe(lateness)

    assert tripwire.max_lateness_ms == 900.0


@pytest.mark.asyncio
async def test_the_app_actually_shows_the_notice_when_the_loop_stalls(caplog, monkeypatch) -> None:
    """Tier 2b: the tripwire is REACHED from the running app, not just correct.

    The unit tests above prove the tripwire computes the right answer. They
    would all pass with nothing wired to it — the shape #3539's own history
    keeps producing (a mechanism that exists and is never reached at the moment
    it is for). So this blocks the event loop for real and asserts the durable
    record — plus, below, that the always-visible chrome row is NOT what got
    written to.
    """
    import logging

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    with caplog.at_level(logging.WARNING, logger="reyn.interfaces.inline.textual_chat.app"):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            # The virtual clock jump simulates the condition being detected
            # — the loop appearing unable to run the watcher — with no real
            # delay.
            status_before = str(app.query_one(StatusLine).render())
            # #4855 follow-up — same determinism fix as the sibling
            # default-visible tests: a real, unrelated stall during mount
            # can already have consumed the App's own tripwire's one-shot
            # _fired latch before this line runs, leaving the
            # "unresponsive" wait below unbounded-hanging instead of
            # observing this test's own stall.
            app.reset_loop_tripwire()
            clock.jump(0.4)
            # #4827 (same class, lead-coder's re-read caught what I missed):
            # a fixed range(6) was trusted as "enough pauses for the stall
            # notice to have been logged" before a positive assert — under
            # real CI-host contention that assert can starve exactly like
            # :794/:902 did. Wait on the real condition instead, unbounded
            # (CLAUDE.md testing policy): if the stall never trips the
            # wire, this waits forever and CI's own --timeout=120 kills
            # it — the detector moved from a separate assert (now removed,
            # six-questions ②: it would have been the same expression on
            # both sides of this loop, unable to ever fail) to that
            # timeout; detection isn't lost, only where it lands.
            while "unresponsive" not in caplog.text:
                await pilot.pause()
            status_after = str(app.query_one(StatusLine).render())

    assert "REYN_PROF_DUMP" in caplog.text, (
        "the record must say how to capture the detail next time"
    )
    # #3668: and the always-visible row is untouched. Measured at 80 columns,
    # appending the notice here took the status line from 62 to 82 characters,
    # which flips ``status_fits_last_row`` and moves the whole segment onto a
    # row of its own — one row of conversation, permanently, bought by a
    # momentary hiccup. On the surface #3680 exists to protect.
    assert status_after == status_before, (
        "a stall changed the always-visible status row: "
        f"{status_before!r} -> {status_after!r}"
    )


@pytest.mark.asyncio
async def test_a_stall_costs_no_row_of_layout(caplog, monkeypatch) -> None:
    """Tier 2b: the notice takes no row from the conversation or the chrome.

    The first fix put it in the flow, the second on the status line. Measured
    at 80 columns, the status-line version took that line from 62 to 82
    characters, which flips ``status_fits_last_row`` and moves the segment onto
    a row of its own — one row of conversation, spent permanently, bought by a
    momentary hiccup, on the surface #3680 exists to protect. So this pins the
    invariant both attempts broke: after a stall, the rows are the rows.

    The delivery itself (an overlay notification) is deliberately NOT asserted
    here: ``run_test`` mounts no toast at all — measured, including for a bare
    ``App`` with a plain ``notify()`` — so a headless assertion on it would be
    testing the harness. It is witnessed in a real terminal instead; see the
    PR body.
    """
    import logging

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    logger_name = "reyn.interfaces.inline.textual_chat.app"
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    with caplog.at_level(logging.WARNING, logger=logger_name):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            rows_before = len(app.query_one(FlowView).entries)
            status_before = str(app.query_one(StatusLine).render())
            merged_before = bool(app.query_one(StatusLine).parent.query(Tab))

            # #4855 follow-up — same determinism fix as the sibling tests
            # in this file: an unrelated real stall during mount can
            # already have consumed the one-shot notice.
            app.reset_loop_tripwire()
            clock.jump(0.4)
            # #4827 (same class as the sibling test above): wait on the
            # real condition, unbounded — if the stall never trips the
            # wire, this waits forever and CI's own --timeout=120 kills
            # it. The separate post-loop assert this replaced would have
            # been the same expression on both sides of this loop
            # (six-questions ②, unable to ever fail) — the detector moved
            # to that timeout, not lost.
            while "unresponsive" not in caplog.text:
                await pilot.pause()

            rows_after = len(app.query_one(FlowView).entries)
            status_after = str(app.query_one(StatusLine).render())
            # The property CI actually caught: #3326 packs the status segment
            # onto the menu row only while it fits. Asserting the RENDERED TEXT
            # alone would miss a longer line that still happens to render, so
            # this asserts the packing outcome itself.
            merged_after = bool(app.query_one(StatusLine).parent.query(Tab))

    assert rows_after == rows_before, (
        "the stall added a conversation row"
    )
    assert status_after == status_before, (
        "the stall changed the always-visible status row, which decides "
        "whether that row merges onto the menu row at 80 columns"
    )
    assert merged_after == merged_before, (
        "the stall changed whether the status segment shares the menu row — "
        "at 80 columns that spends a row of conversation, permanently, on a "
        "momentary hiccup"
    )


# ── #4761 ②: the App pump heartbeat ─────────────────────────────────────────


def test_stall_and_recovered_lines_carry_pump_ticks_when_given() -> None:
    """Tier 2: #4761 ② — the two default-visible notices embed the pump
    heartbeat's value when the caller has one, and stay exactly as they
    were (#4801/#4804's own wording) when it doesn't — this module has no
    idea whether a caller tracks pump ticks at all, so the parameter must
    be fully optional."""
    with_ticks = stall_log_line(1800.0, pump_ticks=42)
    assert "1.8s" in with_ticks
    assert "42" in with_ticks
    without_ticks = stall_log_line(1800.0)
    assert "42" not in without_ticks
    assert without_ticks == (
        "the interface was unresponsive for 1.8s — re-run with "
        "REYN_PROF_DUMP=<path> to record what it was doing"
    )

    recovered_with_ticks = stall_recovered_log_line(pump_ticks=99)
    assert "99" in recovered_with_ticks
    assert stall_recovered_log_line() == "the interface recovered from the stall reported above"


def test_stall_line_carries_a_self_contained_pump_delta() -> None:
    """Tier 2: #4761 ② follow-up (lead-coder review) — a stall that never
    recovers (#4761's own report: the operator killed the process rather
    than waiting) never reaches stall_recovered_log_line's own comparison,
    so H1 needs to be readable from the STALL line alone. pump_delta/
    pump_window_s let it say "the pump advanced by D in the trailing W
    seconds" in the SAME one line — 0 is the H1 signal, a positive delta
    rules H1 out for this episode without a second event."""
    frozen_pump = stall_log_line(1800.0, pump_ticks=12, pump_delta=0, pump_window_s=2.0)
    assert "+0" in frozen_pump
    assert "2s" in frozen_pump

    live_pump = stall_log_line(1800.0, pump_ticks=12, pump_delta=3, pump_window_s=2.0)
    assert "+3" in live_pump

    # Partial info (ticks with no delta/window) falls back to the plain
    # single-value form — a caller that hasn't started tracking a window
    # yet must not have to supply values it doesn't have.
    ticks_only = stall_log_line(1800.0, pump_ticks=12)
    assert "12" in ticks_only
    assert "+" not in ticks_only


def test_observe_threads_pump_ticks_into_both_durable_record_kinds(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: #4761 ② — an armed session's durable trace carries the SAME
    pump-ticks value the caller passed, for both the periodic ``"tripwire"``
    record and the ``"tripwire_recovered"`` one — the comparable pair an
    operator with detail on would need to see whether the count moved
    during an ongoing stall."""
    import json

    target = tmp_path / "probe.jsonl"
    monkeypatch.setenv("REYN_PROF_DUMP", str(target))
    tripwire = LoopTripwire(threshold_ms=250.0)

    tripwire.observe(1800.0, pump_ticks=5)
    tripwire.observe(50.0, pump_ticks=8)

    records = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    by_kind = {r["kind"]: r for r in records}
    assert by_kind["tripwire"]["pump_ticks"] == 5
    assert by_kind["tripwire_recovered"]["pump_ticks"] == 8


@pytest.mark.asyncio
async def test_on_timer_advances_pump_ticks_only_for_its_own_timer() -> None:
    """Tier 2: #4761 ② — the discriminator this mechanism depends on:
    ``on_timer`` must advance ``_pump_ticks`` for the App's OWN heartbeat
    Timer and NOT for an unrelated one, or the counter's meaning drifts
    from "this app's own message pump is alive" to "some timer somewhere
    fired," which the docstring explicitly rules out. Delegation to
    Textual's own base ``on_timer`` (which invokes a Timer's real
    ``callback``, if any) is also exercised here via the two real
    ``self.set_timer(..., callback=...)`` sites elsewhere in this class
    — a broken delegation would leave voice-recording's own timeout or
    the streamed-reply catch-up silently never firing, not just this
    counter untested."""
    from textual import events

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        before = app.pump_ticks
        assert app.pump_heartbeat_timer is not None

        # Our own timer's event: advances the counter.
        await app.on_timer(events.Timer(timer=app.pump_heartbeat_timer, time=0.0, count=1))
        assert app.pump_ticks == before + 1

        # A DIFFERENT timer's event: must NOT advance it — the
        # discriminator itself, not just "some timer event was handled".
        other_timer = app.set_timer(999.0, name="unrelated-probe-timer")
        try:
            await app.on_timer(events.Timer(timer=other_timer, time=0.0, count=1))
            assert app.pump_ticks == before + 1, (
                "on_timer must only advance _pump_ticks for its OWN timer"
            )
        finally:
            other_timer.stop()


@pytest.mark.asyncio
async def test_pump_heartbeat_reaches_the_default_visible_notices(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: #4761 ② — REACHED end to end: a real stall + recovery,
    under the reconstructed real logging floor (no ``caplog``, mirroring
    #4804's own pattern), and the pump-ticks value from the App's real
    ``_pump_ticks`` counter actually lands in both default-visible notices
    — not just that the formatting functions accept the parameter in
    isolation (the sibling unit test above), but that app.py's own call
    site actually supplies it."""
    import logging

    from textual import events

    logfile = tmp_path / "reyn.log"
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    # This test's own "+" assertion below needs a NON-ZERO trailing-window
    # pump-heartbeat delta, which the App's real 1.0s-interval Timer would
    # normally supply — but a real Timer still needs REAL wall-clock time to
    # fire, which the virtual-clock jump below does not provide (it only
    # changes what the tripwire's OWN lateness computation perceives, not
    # real elapsed time). Driving on_timer(...) directly (same technique
    # test_on_timer_advances_pump_ticks_only_for_its_own_timer already
    # uses) advances pump_ticks deterministically, with no dependence on
    # the real Timer's own schedule either.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    try:
        logging.basicConfig(
            filename=str(logfile), level=logging.WARNING, force=True,
        )
        stall_seconds = (loop_probe._TRIPWIRE_MS + 150) / 1000
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.pump_heartbeat_timer is not None
            await app.on_timer(
                events.Timer(timer=app.pump_heartbeat_timer, time=0.0, count=1),
            )
            # #4855: mark where THIS test's own content begins — see the
            # sibling test_keys_received_... 's comment for why (an earlier,
            # unrelated stall's "unresponsive" line is not this test's own).
            pre_jump_len = len(logfile.read_text(encoding="utf-8"))
            # #4855 follow-up (lead-coder's TESTS-READ, same PR): slicing
            # to pre_jump_len (above) fixed WHICH line this test reads —
            # it does not fix whether a "recovered" line for THIS test's
            # own stall gets written at all. A real mount-time stall can
            # already have consumed the App's own tripwire's one-shot _fired
            # latch before this line runs; a fresh instance makes this
            # test's own stall/recovery pair deterministic regardless.
            app.reset_loop_tripwire()
            clock.jump(stall_seconds)
            # #4855 (same defect, same function, 2 lines up): waiting on
            # "recovered" ANYWHERE in the file — not sliced to what THIS
            # test's own jump caused — lets an earlier, unrelated
            # recovery already in the file satisfy the wait before this
            # test's own stall has even been logged, racing the read
            # below against a "unresponsive" line that has not landed
            # yet.
            while "recovered" not in logfile.read_text(encoding="utf-8")[pre_jump_len:]:
                await pilot.pause()
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    content = logfile.read_text(encoding="utf-8")[pre_jump_len:]
    assert "pump heartbeat" in content, (
        f"the pump-ticks reading must reach the real, default-visible log: {content!r}"
    )
    stall_line = next(
        (line for line in content.splitlines() if "unresponsive" in line), None,
    )
    assert stall_line is not None, (
        f"no 'unresponsive' line in this test's own content: {content!r}"
    )
    assert "+" in stall_line, (
        "the STALL line itself (not just the recovery line) must carry the "
        "self-contained trailing-window delta — a freeze that never "
        f"recovers never reaches the recovery line at all: {stall_line!r}"
    )


# ── #4761 ③: the key-arrival counter ────────────────────────────────────────


def test_stall_line_carries_keys_received_and_delta() -> None:
    """Tier 2: #4761 ③ — the stall line gains a THIRD, independently-optional
    field for key arrival, in the same self-contained-in-one-line shape ②'s
    own lead-coder review established (no pairing with a recovery-side
    reading — an H3 diagnosis is needed most on a freeze that never
    resolves)."""
    with_keys = stall_log_line(
        1800.0, pump_ticks=5, pump_delta=2, pump_window_s=2.0,
        keys_received=7, keys_delta=0,
    )
    assert "keys received: 7" in with_keys
    assert "+0" in with_keys  # the H3 signal: pump moved, keys did not

    # keys_received alone (no delta/window) falls back to the bare form —
    # a caller with a value but no window yet must not have to fabricate one.
    ticks_only_no_keys = stall_log_line(1800.0, pump_ticks=5)
    assert "keys received" not in ticks_only_no_keys

    keys_only = stall_log_line(1800.0, keys_received=3)
    assert "keys received: 3" in keys_only
    assert "pump heartbeat" not in keys_only


@pytest.mark.asyncio
async def test_on_event_counts_key_events_only() -> None:
    """Tier 2: #4761 ③ — the discriminator this counter depends on: every
    real Key event reaching the App counts, and nothing else does (a mouse
    scroll, in particular, is handled by the SAME isinstance-guarded block
    in on_event that the overlay-dismiss logic already shares, so this
    pins that the two checks stayed correctly split apart)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        before = app.keys_received

        await pilot.press("a")
        assert app.keys_received == before + 1

        await pilot.press("escape")
        assert app.keys_received == before + 2, (
            "a key that ALSO triggers other on_event handling (escape) "
            "must still be counted — the counter's job is arrival, not outcome"
        )

        from textual import events

        await app.on_event(
            events.MouseScrollDown(None, 0, 0, 0, 0, 0, False, False, False)
        )
        assert app.keys_received == before + 2, (
            "a non-Key event reaching the same on_event method must NOT "
            "advance the key counter"
        )


@pytest.mark.asyncio
async def test_keys_received_reaches_the_default_visible_stall_notice(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: #4761 ③ — REACHED end to end: real keypresses, then a real
    stall, under the reconstructed real logging floor (no ``caplog``,
    mirroring #4804/②'s own pattern) — the key-arrival count from the
    App's real ``on_event`` wiring actually lands in the default-visible
    stall notice, not just that the formatting function accepts the
    parameter in isolation."""
    import logging

    logfile = tmp_path / "reyn.log"
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    try:
        logging.basicConfig(
            filename=str(logfile), level=logging.WARNING, force=True,
        )
        stall_seconds = (loop_probe._TRIPWIRE_MS + 150) / 1000
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("a", "b", "c")
            # #4827①: pilot.press() awaits Textual's own message-queue-drain
            # marker (Pilot._wait_for_screen), which is robust — but under
            # real CI-host CPU contention the WHOLE process (this one, not
            # just key dispatch) can be starved long enough that press()'s
            # return is not yet followed by the counter's own read being
            # observed consistent with it on this exact interpreter turn.
            # Rather than trust press()'s return as an implicit guarantee,
            # wait on the actual fact the rest of this test depends on
            # before entering the timing-sensitive stall phase below — an
            # unbounded condition wait (CLAUDE.md testing policy: no
            # attempts=N / time-bound), never a race the test itself builds.
            while app.keys_received < 3:
                await pilot.pause()
            # #4855: the "unresponsive" line this test reads must be the ONE
            # ITS OWN clock.jump caused — not the first "unresponsive" line
            # anywhere in the file (an unrelated stall during app mount can
            # log one earlier, e.g. real CI-host contention). Position in
            # the file is not identity; what this test itself caused is —
            # so the offset right before the jump marks where THIS test's
            # own content begins. No time is written in either direction.
            pre_jump_len = len(logfile.read_text(encoding="utf-8"))
            # #4855 follow-up — see
            # test_pump_heartbeat_reaches_the_default_visible_notices's
            # identical comment: a fresh LoopTripwire makes this test's
            # own stall/recovery pair deterministic regardless of an
            # unrelated real stall during mount.
            app.reset_loop_tripwire()
            clock.jump(stall_seconds)
            # #4855 (same defect, same function, 2 lines up): sliced to
            # THIS test's own content — see
            # test_pump_heartbeat_reaches_the_default_visible_notices's
            # sibling comment for why an unsliced wait races an
            # unrelated recovery already in the file.
            while "recovered" not in logfile.read_text(encoding="utf-8")[pre_jump_len:]:
                await pilot.pause()
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    content = logfile.read_text(encoding="utf-8")[pre_jump_len:]
    stall_line = next(
        (line for line in content.splitlines() if "unresponsive" in line), None,
    )
    assert stall_line is not None, (
        f"no 'unresponsive' line in this test's own content: {content!r}"
    )
    assert "keys received: 3" in stall_line, (
        f"the 3 real keypresses must show up in the stall notice's own count: {stall_line!r}"
    )


# ── #4761: turn_active, whether a turn was running at the moment ──────────


def test_stall_line_carries_turn_active_when_given() -> None:
    """Tier 2: #4761 (architect's outstanding point) — the STALL notice
    embeds whether a turn was running at the instant it fired, and stays
    exactly as it was when the caller doesn't have that signal — this
    module has no idea whether a caller tracks turn activity at all, so the
    parameter must be fully optional, same discipline as ``pump_ticks``.

    Without this, a byte-identical frozen screen is not evidence of a
    freeze on its own — it is equally consistent with "nothing was
    happening" (idle, no turn in flight)."""
    active = stall_log_line(1800.0, turn_active=True)
    assert "(turn active at the time)" in active
    idle = stall_log_line(1800.0, turn_active=False)
    assert "(turn idle at the time)" in idle
    assert "(turn active at the time)" not in idle
    unspecified = stall_log_line(1800.0)
    assert "turn active" not in unspecified and "turn idle" not in unspecified
    assert unspecified == (
        "the interface was unresponsive for 1.8s — re-run with "
        "REYN_PROF_DUMP=<path> to record what it was doing"
    )


def test_observe_threads_turn_active_into_both_durable_record_kinds(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: #4761 — an armed session's durable trace carries the SAME
    turn-active value the caller passed, for both the periodic
    ``"tripwire"`` record and the ``"tripwire_recovered"`` one — mirrors
    ``pump_ticks``'s own coverage above, same reasoning: consistency for
    the opt-in detail dump, even though the default-visible notice (tested
    separately below) is what actually matters for an unarmed session."""
    import json

    target = tmp_path / "probe.jsonl"
    monkeypatch.setenv("REYN_PROF_DUMP", str(target))
    tripwire = LoopTripwire(threshold_ms=250.0)

    tripwire.observe(1800.0, turn_active=True)
    tripwire.observe(50.0, turn_active=False)

    records = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    by_kind = {r["kind"]: r for r in records}
    assert by_kind["tripwire"]["turn_active"] is True
    assert by_kind["tripwire_recovered"]["turn_active"] is False


@pytest.mark.asyncio
async def test_turn_active_reaches_the_default_visible_stall_notice(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: #4761 — REACHED end to end, under the reconstructed real
    logging floor (no ``caplog``, mirroring #4804's own pattern and this
    file's own ``test_pump_heartbeat_reaches_the_default_visible_notices``):
    a real stall that happens WHILE a turn is running (a real
    ``turn_started`` event pushed through the transport — the same public
    path production uses to set ``ActivityRow``'s state, not a private
    ``_activity.begin()`` reach-around) shows ``turn active`` in the
    default-visible log line, not gated behind ``REYN_PROF_DUMP``."""
    import logging

    logfile = tmp_path / "reyn.log"
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    monkeypatch.delenv("REYN_PROF_DUMP", raising=False)
    # #4844: virtual clock, no real sleep — see _VirtualClock's docstring.
    clock = _VirtualClock()
    monkeypatch.setattr(time, "perf_counter", clock)
    try:
        logging.basicConfig(
            filename=str(logfile), level=logging.WARNING, force=True,
        )
        stall_seconds = (loop_probe._TRIPWIRE_MS + 150) / 1000
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # A real turn_started event — the same public path
            # _handle_turn_started_event uses to call
            # self._activity.begin("WORKING") in production.
            await transport.push_event(
                Event(
                    type="turn_started",
                    data={"kind": "user", "chain_id": "chain-1", "seq": 1},
                )
            )
            # #4827 (same class as ①, folded here): a single pilot.pause()
            # was trusted as "the pushed event has been processed", before
            # immediately entering the real, blocking time.sleep() stall
            # phase below — under real CI-host contention that trust can be
            # wrong (measured: this exact assert failed in CI with
            # "turn idle" instead of "turn active", same run that also hit
            # ①'s failure). Wait on the real fact this test depends on — the
            # App's own turn-activity surface, the same one
            # ``_watch_loop_responsiveness`` reads (``self._activity.state
            # is not None``) — via Textual's public widget query rather
            # than reaching into the private attribute. Unbounded (CLAUDE.md
            # testing policy: no attempts=N / time-bound); CI's own
            # --timeout=120 is the kill switch if the wiring is ever
            # actually broken and this never arrives.
            from reyn.interfaces.inline.textual_chat.activity_row import ActivityRow
            while app.query_one(ActivityRow).state is None:
                await pilot.pause()
            # #4827① recurrence diagnostic (lead-coder): the wait loop above
            # reads `query_one(ActivityRow)`; the tripwire's own tick reads
            # `self._activity` directly (app.py's `getattr(self, "_activity",
            # None)`). "Same object" was only verified structurally
            # (compose() assigns self._activity exactly once, the same
            # object it yields into the tree) — never empirically, on a
            # CI host, at the moment a real failure happened. Captured here,
            # while the app is still alive/mounted (post-teardown queries
            # are unreliable) — used only in the enriched assert message
            # below, so this costs nothing on a passing run.
            _diag_activity_obj_id = id(app._activity)
            _diag_query_obj_id = id(app.query_one(ActivityRow))
            _diag_state_at_confirm = app._activity.state
            # #4855: mark where THIS test's own content begins — see
            # test_keys_received_...'s comment for why (an earlier,
            # unrelated stall's "unresponsive" line is not this test's own).
            pre_jump_len = len(logfile.read_text(encoding="utf-8"))
            # #4855 follow-up (same determinism fix as the sibling
            # default-visible tests, applied here too — this wait targets
            # "unresponsive", not "recovered", but is exposed to the SAME
            # pre-existing hazard: a real, unrelated stall during mount can
            # already have consumed the App's own tripwire's one-shot _fired
            # latch, silently swallowing THIS test's own onset and leaving
            # the wait below unbounded-hanging instead of reading its own
            # stall line).
            app.reset_loop_tripwire()
            clock.jump(stall_seconds)
            while "unresponsive" not in logfile.read_text(encoding="utf-8")[pre_jump_len:]:
                await pilot.pause()
            # lead-coder's TESTS-READ (#4842): these 3 are read AFTER this
            # test's OWN loop above observes "unresponsive" in the logfile —
            # not synchronized with the tripwire's own tick that actually
            # WROTE the line. By the time this runs, further ticks may have
            # already advanced pump_ticks/keys_received past what the
            # tripwire itself saw, and activity.state could in principle
            # have moved again too. Still useful (a divergence AT LEAST this
            # late is still a divergence), but "at observation-time" is the
            # honest name — not "at the tripwire's own read".
            _diag_pump_ticks_after_notice_observed = app.pump_ticks
            _diag_keys_received_after_notice_observed = app.keys_received
            _diag_state_after_notice_observed = app._activity.state
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    content = logfile.read_text(encoding="utf-8")[pre_jump_len:]
    stall_line = next(
        (line for line in content.splitlines() if "unresponsive" in line), None,
    )
    assert stall_line is not None, (
        f"no 'unresponsive' line in this test's own content: {content!r}"
    )
    assert "turn active" in stall_line, (
        "a stall observed while a real turn_started event is in flight must "
        f"say so in the default-visible notice: {stall_line!r}. "
        "#4827① recurrence diagnostic — "
        f"id(self._activity) at confirm-time={_diag_activity_obj_id!r} "
        f"id(query_one(ActivityRow)) at confirm-time={_diag_query_obj_id!r} "
        f"(same object: {_diag_activity_obj_id == _diag_query_obj_id}); "
        f"activity.state at confirm-time={_diag_state_at_confirm!r} "
        f"activity.state after this test observed the notice={_diag_state_after_notice_observed!r}; "
        f"pump_ticks after this test observed the notice={_diag_pump_ticks_after_notice_observed!r} "
        f"keys_received after this test observed the notice={_diag_keys_received_after_notice_observed!r}"
    )
