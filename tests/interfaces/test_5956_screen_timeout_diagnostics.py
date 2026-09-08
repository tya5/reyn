"""Tier 2: #5956 (#4986 variant C's own armed observer) — the diagnostic
collector + catch/re-raise wrapper this issue's own closing condition asks
for. Real ``TextualChatApp`` + real ``Worker``/``LoopTripwire`` state
throughout — no mocks; the whole point is that the 3 diagnostics are read
off a genuinely running app, not synthesised.
"""
from __future__ import annotations

import asyncio

import pytest
from textual.pilot import WaitForScreenTimeout

from reyn.interfaces.inline.textual_chat import TextualChatApp
from tests._support.screen_timeout_diagnostics import (
    collect_screen_timeout_diagnostics,
    diagnosed_screen_wait,
)
from tests.interfaces.test_textual_chat_intervention_panel_3299 import RecordingTransport


def _empty_transport() -> RecordingTransport:
    """A real transport with no messages, stream kept open (``end=False``)
    so the app stays mounted — this file needs nothing from the
    intervention flow itself, only a real, minimal, already-established
    transport double."""
    return RecordingTransport([], end=False)


# ── collect_screen_timeout_diagnostics: real app, real values ───────────


@pytest.mark.asyncio
async def test_collects_real_pump_ticks_and_loop_tripwire_state() -> None:
    """Tier 2: ② and ③ read real, live values off a real running app — not
    placeholders. ``pump_ticks`` is the app's OWN heartbeat-timer counter
    (``TextualChatApp.on_timer``, incremented only when that specific
    timer fires — not on every pump pass), so this asserts it is a real,
    present int (distinguishing it from the ``None`` a broken/absent app
    would report — see ``test_collector_never_raises_...`` below), not
    that it has already advanced past zero on this specific short-lived
    run. The loop tripwire (armed by construction on every
    ``TextualChatApp``) must report its real, healthy (never-fired)
    state on an app that never stalled."""
    app = TextualChatApp(transport=_empty_transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        diagnostics = collect_screen_timeout_diagnostics(app)

        assert isinstance(diagnostics["pump_ticks"], int)
        tripwire = diagnostics["loop_tripwire"]
        assert tripwire["fired"] is False, "a healthy run must not report an active stall"
        assert tripwire["max_lateness_ms"] < tripwire["threshold_ms"], (
            "a healthy run's worst observed lateness must stay under its "
            "own threshold -- both real values, read off the app's own "
            "LoopTripwire, not pinned to an exact synthetic number"
        )
        # A fresh TextualChatApp already runs its own real internal worker
        # (e.g. `_pump_frames`) -- this asserts the SHAPE is right, not
        # emptiness, which would be false on a real app (measured directly
        # while writing this test: it is not "no workers ever," it is
        # "this app's own known-real worker(s), correctly enumerated").
        assert isinstance(diagnostics["pending_workers"], list)
        assert all(
            {"name", "group", "description", "state"} <= w.keys()
            for w in diagnostics["pending_workers"]
        )


@pytest.mark.asyncio
async def test_collects_a_real_pending_worker_by_name() -> None:
    """Tier 2: ① — a worker genuinely still running (never finishing on
    its own within the test) must be named in the collected diagnostics,
    by the SAME name it was started with — proving this reads Textual's
    real ``WorkerManager``, not a stand-in."""
    app = TextualChatApp(transport=_empty_transport())
    async with app.run_test(size=(100, 30)) as pilot:
        never_finishes = asyncio.Event()
        app.run_worker(never_finishes.wait(), name="e2e-5956-marker-worker")
        await pilot.pause()

        diagnostics = collect_screen_timeout_diagnostics(app)

        names = [w["name"] for w in diagnostics["pending_workers"]]
        assert "e2e-5956-marker-worker" in names, (
            f"a genuinely still-running worker must appear by name; got {names!r}"
        )
        never_finishes.set()  # let it finish so the test's own teardown is clean


# ── diagnosed_screen_wait: catch, attach, re-raise -- never swallow ──────


@pytest.mark.asyncio
async def test_a_real_screen_timeout_is_re_raised_with_the_diagnostics_attached() -> None:
    """Tier 2: LOAD-BEARING — the exception is NEVER swallowed (the
    #5909-shaped false-positive risk lead-coder's own dispatch named: an
    observer that fires on ordinary completion, or that eats the real
    signal, is worse than none). Drives a REAL WaitForScreenTimeout by
    raising it from inside the wrapped block -- the correct way to test
    an exception handler's own behaviour, not by forcing Textual's
    internal 30s clock to genuinely expire."""
    app = TextualChatApp(transport=_empty_transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.run_worker(asyncio.Event().wait(), name="e2e-5956-witness-worker")
        await pilot.pause()

        with pytest.raises(WaitForScreenTimeout) as excinfo:
            async with diagnosed_screen_wait(app):
                raise WaitForScreenTimeout("Timed out while waiting for widgets to process pending messages.")

        notes = "\n".join(getattr(excinfo.value, "__notes__", []))
        assert "#5956 diagnostics" in notes, "the note must actually be attached"
        assert "e2e-5956-witness-worker" in notes, (
            "① the real pending worker's name must appear in the record"
        )
        assert "pump_ticks" in notes, "② the pump-tick snapshot must appear in the record"
        assert "loop_tripwire" in notes, "③ the loop-tripwire state must appear in the record"


@pytest.mark.asyncio
async def test_an_unrelated_exception_passes_through_completely_untouched() -> None:
    """Tier 2: deny side (#5909's own lesson, applied here) — the wrapper
    must recognise ONLY ``WaitForScreenTimeout``. Any other exception
    (including a plain bug in the wrapped block) must propagate
    byte-identical, no note attached, no type change -- an observer that
    reacts to cases outside its own named scope is exactly the shape
    that produced #5909's 12 false positives."""
    app = TextualChatApp(transport=_empty_transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        with pytest.raises(ValueError) as excinfo:
            async with diagnosed_screen_wait(app):
                raise ValueError("unrelated bug, not a screen timeout")

        assert str(excinfo.value) == "unrelated bug, not a screen timeout"
        assert not getattr(excinfo.value, "__notes__", []), (
            "an unrelated exception must carry no diagnostics note at all"
        )


@pytest.mark.asyncio
async def test_ordinary_completion_never_fires_the_observer() -> None:
    """Tier 2: the #5909 false-positive shape, directly -- an ORDINARY,
    successful pass through the wrapped block must produce no note, no
    side effect, nothing collected. The observer's cost on the healthy
    path is exactly the ``except`` clause never running."""
    app = TextualChatApp(transport=_empty_transport())
    async with app.run_test(size=(100, 30)) as pilot:
        async with diagnosed_screen_wait(app):
            await pilot.pause()
        # No exception at all -- nothing to assert on notes because there
        # is no exception object; the absence of a raise IS the witness.


# ── collect_screen_timeout_diagnostics never raises on a broken app ─────


def test_collector_never_raises_even_given_a_broken_app_stand_in() -> None:
    """Tier 1: 'an instrument that can break the thing it measures is
    worse than no instrument' (this module's own docstring, mirroring
    loop_tripwire.write_record's rule). An object missing every expected
    attribute must still yield a usable dict, never raise."""

    class _Broken:
        pass

    diagnostics = collect_screen_timeout_diagnostics(_Broken())
    assert diagnostics["pending_workers"] == []
    assert diagnostics["pump_ticks"] is None
    assert diagnostics["loop_tripwire"] is None
