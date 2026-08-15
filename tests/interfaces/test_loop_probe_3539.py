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
    write_record,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransport):
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
async def test_the_app_actually_shows_the_notice_when_the_loop_stalls(caplog) -> None:
    """Tier 2b: the tripwire is REACHED from the running app, not just correct.

    The unit tests above prove the tripwire computes the right answer. They
    would all pass with nothing wired to it — the shape #3539's own history
    keeps producing (a mechanism that exists and is never reached at the moment
    it is for). So this blocks the event loop for real and asserts the durable
    record — plus, below, that the always-visible chrome row is NOT what got
    written to.
    """
    import logging
    import time

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    with caplog.at_level(logging.WARNING, logger="reyn.interfaces.inline.textual_chat.app"):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            # A synchronous sleep: the loop cannot run the watcher while this
            # holds it, which is exactly the condition being detected.
            status_before = str(app.query_one(StatusLine).render())
            time.sleep(0.4)
            for _ in range(6):
                await pilot.pause()
            status_after = str(app.query_one(StatusLine).render())

    assert "unresponsive" in caplog.text, (
        f"the loop stalled and nothing recorded it: {caplog.text!r}"
    )
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
async def test_a_stall_costs_no_row_of_layout(caplog) -> None:
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
    import time

    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    logger_name = "reyn.interfaces.inline.textual_chat.app"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            rows_before = len(app.query_one(FlowView).entries)
            status_before = str(app.query_one(StatusLine).render())
            merged_before = bool(app.query_one(StatusLine).parent.query(Tab))

            time.sleep(0.4)
            for _ in range(6):
                await pilot.pause()

            rows_after = len(app.query_one(FlowView).entries)
            status_after = str(app.query_one(StatusLine).render())
            # The property CI actually caught: #3326 packs the status segment
            # onto the menu row only while it fits. Asserting the RENDERED TEXT
            # alone would miss a longer line that still happens to render, so
            # this asserts the packing outcome itself.
            merged_after = bool(app.query_one(StatusLine).parent.query(Tab))

    # Non-vacuity: without a stall, "nothing moved" is true for the
    # uninteresting reason and this test asserts nothing.
    assert "unresponsive" in caplog.text, (
        "the stall did not trip the wire — raise the sleep or lower the threshold"
    )
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
