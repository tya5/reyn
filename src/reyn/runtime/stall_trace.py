"""#4405 (extended to cover startup too, same issue's own follow-up ask):
an opt-in, off-by-default diagnostic that dumps the process's Python
stack to reyn's own log if reyn blocks longer than ``REYN_STALL_TRACE``
seconds — armed for the whole chat session (startup included,
``chat.py``'s entrypoint) with each turn's own arm/disarm
(``Session._run_router_loop``) nesting safely inside it, so a stall
anywhere — not just mid-turn — gets caught.

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

logger = logging.getLogger(__name__)

_ENV_VAR = "REYN_STALL_TRACE"

#: #4405 startup extension: how many active ``arm()`` callers currently want
#: the timer running. ``faulthandler.dump_traceback_later`` is ONE global
#: timer with no reentrancy of its own — a second ``arm()`` call would just
#: re-point the same timer, and an inner ``disarm()`` would cancel it out
#: from under an outer caller still relying on it. This makes nesting safe:
#: the chat entrypoint arms once for the whole session (startup + every
#: turn), each turn's own arm/disarm (Session._run_router_loop) nests
#: inside it — only the OUTERMOST arm (0 -> 1) touches the real API, only
#: the OUTERMOST disarm (1 -> 0) cancels it. An unbalanced disarm (more
#: disarms than arms) is clamped at 0 rather than going negative, so a
#: stray extra call can't leave the counter lying about whether anything is
#: really armed.
_depth = 0


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


def arm(seconds: float) -> None:
    """Start the background-thread stall timer, or — if a caller further
    out already armed it — just record that this caller wants it running
    too. Pair with :func:`disarm` in a ``finally`` (both the chat
    entrypoint bracketing the whole session and each turn's own
    arm/disarm nest safely; see the module-level ``_depth`` docstring for
    why nesting needs this rather than calling ``faulthandler`` directly).
    """
    global _depth
    if _depth == 0:
        faulthandler.dump_traceback_later(seconds, repeat=True, file=_log_stream(), exit=False)
    _depth += 1


def disarm() -> None:
    """Release one ``arm()`` call. Cancels the real timer only once every
    caller that armed it has released — safe to call even if nothing is
    armed (clamped at 0, never goes negative)."""
    global _depth
    if _depth == 0:
        return
    _depth -= 1
    if _depth == 0:
        faulthandler.cancel_dump_traceback_later()
