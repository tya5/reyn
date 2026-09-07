"""#5956 (#4986 variant C's own armed observer) — the FIRST real diagnostic
data a `textual.pilot.WaitForScreenTimeout` occurrence gives this repo.

Owner-hit incident recap (#4986): a `WaitForScreenTimeout` fired once, in
`tests/interfaces/test_textual_chat_intervention_panel_3299.py`, on an
unrelated PR's CI run (#5331). Structurally different from variants A/B (a
Textual-internal timeout, not `pytest-timeout`/an `asyncio` event-loop
stall) — #4051/#4052 do not explain it (verified directly against that
run's own log, not inferred). Architect's own closing condition for this
issue (quoted at #5956's own filing): "catch it and attach ①a pending
Textual worker ②the pump's own last-processed tick ③whether the loop
tripwire fired — that trio settles the mechanism on the NEXT occurrence."

## Why THIS shape, not a duration

CLAUDE.md's own testing policy forbids writing a duration a test's
assertion depends on, in EITHER direction — and this issue's own dispatch
repeats it explicitly ("duration は書かない — timeout を待つ形の issue な
ので最も流れやすい"). Nothing here measures HOW LONG the timeout took, or
waits for one to happen: :func:`diagnosed_screen_wait` is a passive catch
around an exception `textual.pilot._wait_for_screen` already raises on its
OWN internal 30s clock — this module never starts a clock of its own,
never asserts a magnitude, and adds nothing to the wait itself (the
`except`/`add_note`/`raise` path costs nothing on the healthy path, which
never executes it).

## The three diagnostics, and where each one comes from

① **pending Textual worker(s)** — `app.workers` (Textual's own
`WorkerManager`, already tracking every `run_worker()` call), filtered to
`not worker.is_finished`. A worker still running (or merely PENDING) at
the moment `_wait_for_screen` gave up is a live candidate for what it was
actually waiting on — Textual's own `call_later`/message-pump completion
tracking (`_wait_for_screen`'s internal counter) is a DIFFERENT mechanism
from the Worker API, so a stuck worker would not itself block the pump
directly, but a widget's `on_mount`/message handler blocking ON a worker
result (`await worker.wait()`) would show up here as "pump still has
something outstanding" with no `call_later` counterpart to explain it.

② **the pump's own last-processed tick** — `TextualChatApp._pump_ticks`
(#4761 ②'s own message-pump heartbeat counter, already built and already
armed by the loop tripwire machinery this app carries by construction —
reused here, not reinvented). A snapshot at catch time, nothing more; a
LATER occurrence comparing this value against activity elsewhere in the
same CI log is what would show whether the pump was still advancing.

③ **whether the loop tripwire fired** — `TextualChatApp._loop_tripwire`
(:class:`reyn.runtime.loop_tripwire.LoopTripwire`, #3539/#5870/#5898),
already watching this app's own event-loop responsiveness on every tick.
If it had already fired (or its own worst-observed lateness is nonzero)
at the moment `_wait_for_screen` gave up, the SAME event loop this pilot
call needed was independently already flagged as unresponsive — variant
B's own mechanism (#5445), reused as a cross-check for variant C rather
than asserted to be the SAME issue (#4986's own repeated discipline:
never merge two incidents on symptom-resemblance alone).

## What this does NOT claim

A record here does not prove causation — it is the primary data #4986's
own closing condition asked for, to be read against whatever the NEXT
real occurrence's own log shows. `collect_screen_timeout_diagnostics` is
exposed separately from :func:`diagnosed_screen_wait` so a test can
assert on the DATA it collects directly, without needing to force a real
Textual-internal timeout to do it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator


def collect_screen_timeout_diagnostics(app: Any) -> "dict[str, Any]":
    """Snapshot ①②③ off *app* right now — best-effort, never raising: a
    diagnostic collector that can itself fail would destroy the exception
    it was trying to annotate (the same "an instrument that can break the
    thing it measures is worse than no instrument" rule
    ``loop_tripwire.write_record`` already follows).

    Returns a plain dict (JSON-shaped values only) so the caller can
    render it into an exception note, a log line, or a durable record
    with no further translation."""
    diagnostics: "dict[str, Any]" = {}

    try:
        workers = getattr(app, "workers", None)
        pending = [
            {
                "name": worker.name or "",
                "group": worker.group,
                "description": worker.description,
                "state": worker.state.name,
            }
            for worker in (workers or [])
            if not worker.is_finished
        ]
        diagnostics["pending_workers"] = pending
    except Exception as exc:  # noqa: BLE001 - collecting context must never itself raise
        diagnostics["pending_workers_error"] = repr(exc)

    try:
        diagnostics["pump_ticks"] = getattr(app, "_pump_ticks", None)
    except Exception as exc:  # noqa: BLE001
        diagnostics["pump_ticks_error"] = repr(exc)

    try:
        tripwire = getattr(app, "_loop_tripwire", None)
        if tripwire is not None:
            diagnostics["loop_tripwire"] = {
                "fired": tripwire.fired,
                "max_lateness_ms": tripwire.max_lateness_ms,
                "threshold_ms": tripwire.threshold_ms,
            }
        else:
            diagnostics["loop_tripwire"] = None
    except Exception as exc:  # noqa: BLE001
        diagnostics["loop_tripwire_error"] = repr(exc)

    return diagnostics


@asynccontextmanager
async def diagnosed_screen_wait(app: Any) -> "AsyncGenerator[None, None]":
    """Wrap a block that awaits Pilot calls (``pilot.pause()``/``pilot.
    press()``/etc.) that can raise ``textual.pilot.WaitForScreenTimeout``.

    On that SPECIFIC exception, attaches :func:`collect_screen_timeout_
    diagnostics`'s own snapshot as a note (``BaseException.add_note``,
    stdlib, Python 3.11+ — both CI legs this repo runs) and re-raises the
    SAME exception object — never swallowed, never replaced with a
    different type, so pytest's own failure report, and anything else
    that inspects the exception, sees exactly what Textual raised, plus
    the note.

    Any OTHER exception (including one Textual itself raises for a
    different reason) passes through completely untouched — this context
    manager's only job is to recognise ONE specific exception type and
    attach data to it; it must never become a general-purpose except-and-
    reraise wrapper (that would risk the #5909 shape lead-coder's own
    dispatch named: an observer that fires on cases it was never meant to
    watch)."""
    from textual.pilot import WaitForScreenTimeout

    try:
        yield
    except WaitForScreenTimeout as exc:
        diagnostics = collect_screen_timeout_diagnostics(app)
        exc.add_note(f"#5956 diagnostics (variant C observer): {diagnostics!r}")
        raise
