"""#4405: an opt-in, off-by-default diagnostic that dumps the process's
Python stack to reyn's own log if a bracketed span blocks longer than
``REYN_STALL_TRACE`` seconds.

#3671 follow-up: two independent callers arm/disarm this — a turn
(``Session._run_turn_body``, the original #4405 use) and the TUI startup
path (``run_textual_chat``/``TextualChatApp.on_mount``, bracketing the
same ``tui-boot`` span ``startup_timing.py`` already names). Only one
timer exists process-wide (:func:`arm` re-points the SAME global
``faulthandler`` timer — see its own docstring); the startup bracket
disarms at first frame, structurally before an interactive TUI turn can
begin (a turn needs the composer, which needs the app already mounted),
so the two callers never hold an ARMED timer at the same moment on that
path. An entrypoint that never runs the TUI startup sequence at all
(headless/dogfood turns) simply never arms the startup bracket in the
first place — no concurrency to reason about either way.

Born out of #4403's investigation: four independent, real, measured
hypotheses for an owner-reported ~20s per-message freeze were each
individually confirmed AS DEFECTS and each individually FALSIFIED as the
explanation for the 20 seconds (#4398's cooldown, tiktoken timeout absence,
``build_history``'s full per-turn token-estimate scan, #4401's MCP probe
timeout — none of them, alone or combined with the measurements taken,
account for the observed duration). Guessing structurally-plausible causes
had run out of leverage; this tool exists to stop guessing and let reyn
report what it is actually doing while frozen.

**Why ``faulthandler.dump_traceback_later`` and nothing async-based**: it
runs on a dedicated OS thread with its own timer, entirely OUTSIDE the
asyncio event loop — so it fires even when the event loop itself is the
thing that's blocked (a synchronous call on the loop's own thread), which is
exactly the symptom being chased ("animation frozen" = nothing on the loop
is running, including the render tick). An `asyncio.sleep`-based watchdog
task would never get scheduled under the same conditions it's meant to
catch.

**Why stack-only, no argument values or message content**: the owner is on
a company machine and cannot paste log bodies out — the only usable report
back is "the stack shows function X blocking on Y", which is exactly what
``faulthandler``'s traceback dump gives without needing to touch the
payload.

**Default off, zero behavior change**: nothing in this module runs unless
``REYN_STALL_TRACE`` is set to a positive number. No test pins the actual
N-second-block-then-dump behavior (that would need a real multi-second
sleep in a test, which the owner's own timeout policy bans) — this is a
diagnostic tool, not a system invariant, so it carries no Tier per
testing.ja.md's six-question check ① (a tool has no behavior/contract of
its own to protect). Whether the ``REYN_STALL_TRACE`` → arm/disarm WIRING
itself is exercised by a test is tracked separately, not claimed here —
see the PR this module landed in for the current status.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO

logger = logging.getLogger(__name__)

_ENV_VAR = "REYN_STALL_TRACE"


def stall_trace_seconds_from_env() -> "float | None":
    """The configured stall threshold in seconds, or ``None`` if the env var
    is unset, empty, non-numeric, or non-positive — every one of those
    means "off", the default, so a caller need only check for ``None``
    rather than distinguish why it's off."""
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; stall-trace diagnostic stays disabled",
            _ENV_VAR, raw,
        )
        return None
    return seconds if seconds > 0 else None


def _log_stream():
    """The file object reyn's own interactive logging setup
    (``chat.py``'s ``_setup_interactive_logging``) already opened for
    ``.reyn/logs/reyn.log`` — so a stall dump lands in the SAME file the
    operator already knows to check, not a separate file or a bare stderr
    write a terminal UI would corrupt. Falls back to stderr when no
    ``FileHandler`` is installed (non-interactive entrypoints, tests) —
    still visible, just not routed to the log file."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and handler.stream is not None:
            return handler.stream
    return sys.stderr


def arm(seconds: float, *, file: "IO[str] | int | None" = None) -> None:
    """Start the background-thread stall timer. Call at turn entry, paired
    with :func:`disarm` in a ``finally`` so a turn that raises still
    disarms it — never call twice without a ``disarm`` in between
    (``faulthandler`` itself has no such reentrancy guard; overlapping
    ``arm`` calls would just re-point the SAME single global timer at a
    new *seconds*/file, which is not this module's problem to solve since
    only one turn ever runs at a time on a given event loop by
    construction).

    ``file`` (#4986): overrides the destination — default ``None`` keeps
    every existing caller's behavior (:func:`_log_stream`'s own
    resolution) byte-identical. A caller arming this OUTSIDE reyn's own
    runtime, where ``sys.stderr``/a reyn log handler may not be the real
    destination it looks like (pytest's capture manager can own
    ``sys.stderr`` for parts of a session — the same hazard
    ``memory_ceiling.py`` already documents for its own kill message),
    should pass its own already-open file object instead — see
    ``reyn.dev.testing.stall_dump`` for that caller.
    """
    faulthandler.dump_traceback_later(
        seconds, repeat=True, file=file if file is not None else _log_stream(), exit=False
    )


def disarm() -> None:
    """Cancel the timer armed by :func:`arm`. Safe to call even if nothing
    is armed (``faulthandler.cancel_dump_traceback_later`` is itself a
    no-op in that case)."""
    faulthandler.cancel_dump_traceback_later()
