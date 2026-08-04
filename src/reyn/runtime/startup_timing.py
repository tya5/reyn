"""Where startup time goes, reported ON SCREEN (#3671).

An operator reported `reyn chat` taking minutes to reach the TUI on Windows /
git-bash with a fresh ``.reyn``. Nothing in reyn could say where it went: the
audit log's first event is emitted after the Session exists, and **the measured
93% of startup happens before that** (3.46 s of 3.46, with 3.1 s unrecorded on
the machine where it was measured). That blind spot is not specific to this
report — every "startup is slow" report lands in it.

**Reported on screen, not to a file.** The operator cannot copy data off the
machine where the symptom happens. A dump file is therefore not a diagnostic
here, it is a dead end; the output has to be readable in the terminal, short
enough to relay by voice if that is all that is available.

**Off unless asked.** Startup happens on every run, so — unlike the loop
tripwire in #3539 — there is no "the moment arrives unannounced" argument for
paying anything by default. ``REYN_STARTUP_TIMING=1`` turns it on. When off,
:func:`stage` is a context manager that stores one monotonic subtraction and
does nothing else; nothing is formatted and nothing is printed.

**Stages are declared, not discovered.** A phase that never runs still appears,
at 0.00 s, because "this step took no time" and "this step is missing from the
report" are different findings and the second one is the more interesting. A
report that silently omits what did not happen cannot distinguish them.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

_ENV = "REYN_STARTUP_TIMING"

#: When this module was first imported — the earliest instant reyn's own code
#: can observe. Everything before it (the interpreter starting, and the import
#: tree that reaches here) is attributed to ``import``: reyn cannot time what
#: ran before reyn existed, and pretending otherwise would put that cost in
#: ``unaccounted``, where it would look like a mystery rather than the one
#: phase whose cost is structural.
_MODULE_IMPORTED_AT = time.perf_counter()


#: When the interface first reached the screen, or ``None`` while it has not.
#: Startup ENDS here. Measuring to process exit instead folds the whole chat
#: session into the report — an early wiring did exactly that and produced
#: "first-frame 98.5%", which was true and told nobody anything.
_FIRST_FRAME_AT: "float | None" = None


def mark_first_frame() -> None:
    """Record that the interface is now on screen.

    Idempotent: only the FIRST call counts. A surface that re-mounts (a session
    switch, a resize that rebuilds the app) must not move the end of startup to
    a later moment and shrink every share that came before it.
    """
    global _FIRST_FRAME_AT
    if _FIRST_FRAME_AT is None:
        _FIRST_FRAME_AT = time.perf_counter()


def process_elapsed_seconds() -> float:
    """Seconds from import to the interface appearing.

    Falls back to "now" when the interface never appeared — a startup that
    failed or was interrupted still has a wall time worth reporting, and it is
    the case where the report matters most.
    """
    end = _FIRST_FRAME_AT if _FIRST_FRAME_AT is not None else time.perf_counter()
    return end - _MODULE_IMPORTED_AT

#: The startup stages, in the order they run. Declared here rather than
#: collected as they report, so the output has a stable shape an operator can
#: read the same way twice — and so a stage that did not run is visible as
#: ``0.00`` rather than as a missing line.
STAGES: "tuple[str, ...]" = (
    "import",
    "config",
    "registry",
    "plugins",
    "mcp",
    "session",
    "first-frame",
)


def enabled() -> bool:
    """Whether timing is on.

    Read per call rather than captured at import: the flag is checked a handful
    of times over a whole startup, and reading it live means a caller can be
    told to set it without also being told where in the process to set it.
    """
    return os.environ.get(_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class StartupTiming:
    """Accumulates per-stage elapsed time and renders it as lines.

    Additive by construction: an unknown stage name is recorded rather than
    rejected. A diagnostic that raises on an unexpected input destroys the
    startup it exists to describe, and the operator running it is already
    dealing with one problem.
    """

    def __init__(self) -> None:
        self._elapsed: "dict[str, float]" = {}

    def record(self, stage: str, seconds: float) -> None:
        """Add *seconds* to *stage*. Repeated stages accumulate.

        Accumulating rather than overwriting is what makes a repeated step
        visible as its total: #3671's own A-list includes config being read
        more than once, and a report that kept only the last read would hide
        exactly that.
        """
        self._elapsed[stage] = self._elapsed.get(stage, 0.0) + max(0.0, seconds)

    @property
    def total_seconds(self) -> float:
        return sum(self._elapsed.values())

    def unaccounted_seconds(self, wall_seconds: float) -> float:
        """Wall time the stages do not explain.

        The number that matters most when a report disappoints: if the stages
        sum to 0.4 s of a 40 s startup, the answer is not in any of them, and
        saying so beats presenting a tidy breakdown of the wrong 1%.
        """
        return max(0.0, wall_seconds - self.total_seconds)

    def report_lines(self, wall_seconds: float) -> "list[str]":
        """The report, as short lines meant to be read off a screen.

        One stage per line with its share, then the total and whatever is left
        over. Shares are of WALL time rather than of the measured sum, so a
        stage reading 5% of a startup dominated by something unmeasured cannot
        be misread as 5% of the problem.
        """
        lines = ["startup timing (REYN_STARTUP_TIMING=1)"]
        for stage in STAGES:
            seconds = self._elapsed.get(stage, 0.0)
            share = (seconds / wall_seconds * 100) if wall_seconds > 0 else 0.0
            lines.append(f"  {stage:<12} {seconds:6.2f}s  {share:5.1f}%")
        for stage in sorted(set(self._elapsed) - set(STAGES)):
            seconds = self._elapsed[stage]
            share = (seconds / wall_seconds * 100) if wall_seconds > 0 else 0.0
            lines.append(f"  {stage:<12} {seconds:6.2f}s  {share:5.1f}%  (undeclared)")
        unaccounted = self.unaccounted_seconds(wall_seconds)
        share = (unaccounted / wall_seconds * 100) if wall_seconds > 0 else 0.0
        lines.append(f"  {'unaccounted':<12} {unaccounted:6.2f}s  {share:5.1f}%")
        lines.append(f"  {'TOTAL':<12} {wall_seconds:6.2f}s")
        return lines


#: The process-wide record. One startup per process, so a module-level instance
#: is the honest shape — threading it through every construction site would be
#: a lot of plumbing for a diagnostic that is off by default.
TIMING = StartupTiming()


@contextmanager
def stage(name: str) -> "Iterator[None]":
    """Time one startup stage.

    Costs one ``perf_counter`` pair whether or not timing is enabled — the
    stages are a handful of calls across a whole startup, so gating the
    measurement itself would save nothing measurable and add a branch to read.
    What the flag gates is the REPORT.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        TIMING.record(name, time.perf_counter() - started)
