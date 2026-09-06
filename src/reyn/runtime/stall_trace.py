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

#5870 stage 1, a THIRD caller, and it is NOT independent of the other
two the way they are of each other: ``TextualChatApp._watch_loop_
responsiveness`` (:mod:`reyn.interfaces.inline.textual_chat.loop_probe`'s
own always-on tripwire) re-arms this SAME global timer on every tick,
``repeat=False``, from the moment the ``tui-boot`` bracket disarms
(handoff at first frame, immediately before the tripwire's own worker
starts — see ``app.py``'s ``on_mount``) until the App itself exits. From
first frame onward this makes the tripwire the PERMANENT occupant of the
one process-wide timer for an interactive TUI session: a mid-session
turn's own ``REYN_STALL_TRACE``-gated :func:`arm` call (the SECOND
caller, ``Session._run_turn_body``) still runs, but its chosen *seconds*
is overwritten by the tripwire's own next re-arm within one tick
(``_TICK_SECONDS`` — 50 ms by default), so a turn-scoped
``REYN_STALL_TRACE=N`` threshold different from the tripwire's own 250 ms
is effectively unobservable once a TUI session has reached first frame.
This is accepted, not a defect to fix here (architect ruling, #5870): the
tripwire's own always-on stack dump already covers what
``REYN_STALL_TRACE`` existed to catch on this ONE entrypoint, without the
manual opt-in a freeze arriving unannounced could never satisfy in time
— ``REYN_STALL_TRACE`` keeps its original, undiminished meaning on every
OTHER entrypoint (headless/dogfood turns, which never run the TUI
startup sequence and so never compete for the timer at all).

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
from pathlib import Path
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


def find_file_handler_path() -> "str | None":
    """The ``baseFilename`` of the root logger's own installed
    ``FileHandler`` (``.reyn/logs/reyn.log``, when ``chat.py``'s
    ``_setup_interactive_logging`` has run), or ``None`` when no such
    handler is installed.

    A READ-ONLY lookup — deliberately returns the PATH, never the
    handler's own ``stream``/``fileno()``. #5877 (architect finding,
    real-machine measurement): ``faulthandler.dump_traceback_later``
    captures the ``file`` argument's underlying FILE-DESCRIPTOR NUMBER
    at ARM time, not a live object reference — reproduced directly on
    this host: ``open("a")`` → arm → ``close("a")`` (frees the fd
    number) → ``open("b")`` (the OS hands the SAME fd number back out)
    → the pending timer's dump lands in ``b``, not ``a``. A caller that
    stays armed across more than one of ITS OWN calls (this module's
    #5870 stage 1 dead-man's-switch caller) cannot safely borrow the
    handler's own ``stream`` — a log-rotation reopen (#5873) or, under
    pytest, a per-test fd-capture open/close cycle can free and reuse
    that number between two of the caller's own arm calls, silently
    redirecting the NEXT dump into whatever the OS handed that number to
    next (a socket, in the CI hang this finding explains). The fix is
    for such a caller to open its OWN fd against this path
    (``os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)``) and hold
    it for its own entire armed lifetime — a self-opened fd is immune to
    a HANDLER-side rotation reopen (it still points at the original
    inode after the handler's own path is renamed out from under it;
    the next rollover's writes simply land in the renamed file instead —
    harmless, the dump is still readable, just possibly one rollover
    behind) and is never touched by anything else's open/close churn.

    Matches ONLY a ``FileHandler`` whose own path ends in ``.reyn/logs/
    reyn.log`` — never "any ``FileHandler``". Measured directly (a real
    pytest run, not assumed): pytest's OWN logging plugin unconditionally
    installs a ``logging.FileHandler`` SUBCLASS pointed at ``/dev/null``
    on the root logger, for its own internal log-capture/routing — an
    early, naive "first ``FileHandler`` wins" version of this function
    picked up THAT handler instead of a test-installed reyn one (or,
    absent one, armed against ``/dev/null`` rather than genuinely
    skipping — silently defeating the "no stable destination -> no arm
    at all" property this whole fix depends on). This path-shape check
    is what makes pytest's own handler, and any other unrelated
    ``FileHandler`` a future plugin might install, transparent to this
    lookup."""
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        path = Path(handler.baseFilename)
        if path.name == "reyn.log" and path.parts[-3:-1] == (".reyn", "logs"):
            return handler.baseFilename
    return None


def default_log_stream():
    """The file object reyn's own interactive logging setup
    (``chat.py``'s ``_setup_interactive_logging``) already opened for
    ``.reyn/logs/reyn.log`` — so a stall dump lands in the SAME file the
    operator already knows to check, not a separate file or a bare stderr
    write a terminal UI would corrupt. Falls back to the process's
    ORIGINAL stderr (``sys.__stderr__``, #5877 architect ruling) when no
    ``FileHandler`` is installed — never the reassignable ``sys.stderr``
    name, which pytest's own capture manager (and anything else that
    wraps stdio) can rebind mid-session; a caller that armed against
    ``sys.stderr`` before such a rebind would, per :func:`find_file_
    handler_path`'s own finding, dump into whatever fd number the OLD
    stream's close freed up and the rebind's open reused — ``sys.
    __stderr__``'s own fd (2) is never closed for the process's
    lifetime (a capture wrapper dup2's a NEW target onto fd 2 rather
    than closing it), so this fallback carries none of that risk.

    Safe for the ORIGINAL two callers (a turn, or the ``tui-boot`` span):
    both arm ONCE and disarm PROMPTLY (a single turn's duration, or
    startup) — a caller that re-arms in a long-lived loop must open and
    hold its OWN fd instead (:func:`find_file_handler_path` + a caller-
    owned ``os.open``, never arming at all when no handler exists) — see
    ``TextualChatApp._watch_loop_responsiveness`` for that shape.

    Matches the SAME reyn.log-shaped path :func:`find_file_handler_path`
    does, for the SAME reason (measured directly): pytest's own logging
    plugin unconditionally installs its OWN ``logging.FileHandler``
    subclass pointed at ``/dev/null``; an earlier, naive "first
    ``FileHandler`` with a live stream" version of this function picked
    THAT one up under any test exercising a turn/boot arm — silently
    discarding whatever it dumped rather than reaching ``sys.__stderr__``
    (still visible) or a real ``reyn.log`` (if one existed)."""
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler) or handler.stream is None:
            continue
        path = Path(handler.baseFilename)
        if path.name == "reyn.log" and path.parts[-3:-1] == (".reyn", "logs"):
            return handler.stream
    return sys.__stderr__


def arm(seconds: float, *, file: "IO[str] | int | None" = None, repeat: bool = True) -> None:
    """Start the background-thread stall timer. Call at turn entry, paired
    with :func:`disarm` in a ``finally`` so a turn that raises still
    disarms it — never call twice without a ``disarm`` in between
    (``faulthandler`` itself has no such reentrancy guard; overlapping
    ``arm`` calls would just re-point the SAME single global timer at a
    new *seconds*/file, which is not this module's problem to solve since
    only one turn ever runs at a time on a given event loop by
    construction).

    ``file`` (#4986): overrides the destination — default ``None`` keeps
    every existing caller's behavior (:func:`default_log_stream`'s own
    resolution) byte-identical. A caller arming this OUTSIDE reyn's own
    runtime, where ``sys.stderr``/a reyn log handler may not be the real
    destination it looks like (pytest's capture manager can own
    ``sys.stderr`` for parts of a session — the same hazard
    ``memory_ceiling.py`` already documents for its own kill message),
    should pass its own already-open file object instead — see
    ``reyn.dev.testing.stall_dump`` for that caller.

    ``repeat`` (#5870 stage 1): the original two callers both want ONE
    continuous alarm firing every *seconds* for as long as the bracketed
    span runs (``repeat=True``, the default, unchanged) — a turn or the
    ``tui-boot`` span is a single continuous thing to watch. The tripwire
    (:mod:`...loop_probe`'s ``LoopTripwire``, via ``TextualChatApp.
    _watch_loop_responsiveness``) wants the OPPOSITE shape: a dead-man's
    switch it re-arms itself every tick with ``repeat=False`` — each
    re-arm cancels and replaces the PENDING one-shot timer (:func:`disarm`
    is exactly ``cancel_dump_traceback_later``, the same call this
    re-arm's own implicit cancel-then-set uses), so the timer only ever
    actually FIRES when a tick fails to arrive in time to re-arm it before
    the deadline — i.e., while the very thing it's watching is blocked.
    ``repeat=True`` here would instead dump on a fixed cadence regardless
    of whether the loop was ever actually late, which answers a different
    question (this module's original design goal, "the span is taking a
    while") than the tripwire's own ("did THIS tick specifically stall").
    """
    destination = file if file is not None else default_log_stream()
    if destination is None:
        # sys.__stderr__ itself is None (an embedded/frozen interpreter
        # with no attached stderr at all, e.g. `-S`) — genuinely nothing
        # to write to; matches faulthandler's own "needs a real
        # destination" contract rather than passing it a null file.
        return
    faulthandler.dump_traceback_later(seconds, repeat=repeat, file=destination, exit=False)


def disarm() -> None:
    """Cancel the timer armed by :func:`arm`. Safe to call even if nothing
    is armed (``faulthandler.cancel_dump_traceback_later`` is itself a
    no-op in that case)."""
    faulthandler.cancel_dump_traceback_later()
