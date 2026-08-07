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

#: #3735: the share of TOTAL, at or above which `unaccounted` gets its own
#: warning line in the report rather than sitting quietly among the other
#: rows. 20% is well above ordinary measurement slop (scheduler jitter, GC
#: pauses) but well below the 62% the #3735 regression actually produced —
#: chosen to catch "a bracket stopped covering its span" without flagging
#: routine noise.
_UNACCOUNTED_GAP_WARNING_PCT = 20.0

def _origin() -> float:
    """The startup clock's zero.

    ``reyn.STARTUP_CLOCK_ORIGIN`` when available — the earliest reyn code to
    run. Falling back to now would silently report the import phase as 0.00s,
    which is how the first attempt at this failed: the timing module is
    imported LATE, so a clock started here begins after the thing it is meant
    to measure has already finished.
    """
    try:
        import reyn

        return reyn.STARTUP_CLOCK_ORIGIN
    except Exception:  # noqa: BLE001 - a diagnostic must not break a startup
        return time.perf_counter()


#: When reyn's own code first ran — the earliest instant reyn's own code
#: can observe. Everything before it (the interpreter starting, and the import
#: tree that reaches here) is attributed to ``import``: reyn cannot time what
#: ran before reyn existed, and pretending otherwise would put that cost in
#: ``unaccounted``, where it would look like a mystery rather than the one
#: phase whose cost is structural.
_MODULE_IMPORTED_AT = _origin()


#: When the interface first reached the screen, or ``None`` while it has not.
#: Startup ENDS here. Measuring to process exit instead folds the whole chat
#: session into the report — an early wiring did exactly that and produced
#: "first-frame 98.5%", which was true and told nobody anything.
_FIRST_FRAME_AT: "float | None" = None


#: When ``run()`` was reached — everything before it is interpreter start plus
#: the import tree. Measured on this machine: 1.75s of that is ``litellm``
#: alone, against a 1.9s startup. It is the single largest phase and it is not
#: reyn's own work, which is exactly why it needs a line of its own rather than
#: dissolving into ``unaccounted``.
_CLI_REACHED_AT: "float | None" = None


def mark_cli_reached() -> None:
    """Record that the CLI entry point is running.

    Closes the ``import`` stage: the span from this module being imported (the
    earliest instant reyn can observe) to the command actually starting.
    """
    global _CLI_REACHED_AT
    if _CLI_REACHED_AT is None:
        _CLI_REACHED_AT = time.perf_counter()
        TIMING.record("import", _CLI_REACHED_AT - _MODULE_IMPORTED_AT)


#: When the TUI object was constructed — the boundary between reyn assembling
#: things and the terminal framework starting up. Measured here: everything
#: before it totals ~0.35s and everything after it ~1.8s, so a report stopping
#: at ``session`` says nothing about the majority of the wait.
_APP_CONSTRUCTED_AT: "float | None" = None


#: When the async entry point started running. Measured: 0.49s in, with the TUI
#: object not constructed until 1.85s — so ~1.36s sits between them, in session
#: setup, and it was the largest single block of ``unaccounted``.
_ASYNC_ENTERED_AT: "float | None" = None


def mark_async_entered() -> None:
    """Record that the async startup path has begun."""
    global _ASYNC_ENTERED_AT
    if _ASYNC_ENTERED_AT is None:
        _ASYNC_ENTERED_AT = time.perf_counter()


#: The narrow ``client-prep:*`` brackets `mark_app_constructed` sums to
#: compute ``client-prep:other`` (#3735 regression fix) — every named
#: sub-stage of the wide ``mark_async_entered`` → ``mark_app_constructed``
#: span EXCEPT ``client-prep:other`` itself (summing that too would be
#: circular: it is defined as what these do not already cover).
#:
#: ``litellm-import`` briefly lived here (#3671 follow-up, after a real-run
#: measurement showed the original 4 brackets left ~59-60% of the wide span
#: in ``client-prep:other``, traced to ``llm.py``'s ``run_async`` forcing
#: ``import litellm`` before any of the other 4 brackets even started) — and
#: was REMOVED, not renamed, once that forced import itself was removed
#: (``reyn.llm.litellm_bootstrap`` module docstring): a session that never
#: calls the LLM no longer imports litellm at all during startup, so there
#: is no longer a client-prep cost of that name to bracket. The time it used
#: to measure moved out of ``client-prep:other`` entirely rather than into
#: this tuple.
_CLIENT_PREP_NAMED_STAGES: "tuple[str, ...]" = (
    "client-prep:transport",
    "client-prep:read-model",
    "client-prep:tui-import",
    "client-prep:app-construct",
)


#: When the lazily-imported ``run_textual_chat`` module finished importing
#: (``textual``/``textual_flowview`` and everything they pull in — NOT
#: covered by the ``import`` stage above, which closes at ``mark_cli_reached``
#: and therefore only ever measures reyn's OWN import tree; this import is
#: lazy specifically so the flowview/textual cost is paid only on the path
#: that needs it, which means that cost falls INSIDE this span instead).
#: #3671 client-prep breakdown (architect's design): the boundary between
#: "reyn resolved which renderer to use" and "the TUI framework's own object
#: graph is being built" — P3/P4 below.
_TUI_IMPORT_DONE_AT: "float | None" = None


def mark_tui_import_done() -> None:
    """Record that the lazy ``textual_chat``/``textual``/``textual_flowview``
    import (``client_driver.py``'s ``from reyn.interfaces.inline.textual_chat
    import run_textual_chat``) has finished.

    Paired with :func:`mark_app_constructed` to bracket ``client-prep:app-
    construct`` (P4) — the same "call is being made and returns are one
    ``await`` deeper than a ``with`` block can hold" shape as
    ``mark_app_constructed``/``mark_first_frame``.
    """
    global _TUI_IMPORT_DONE_AT
    if _TUI_IMPORT_DONE_AT is None:
        _TUI_IMPORT_DONE_AT = time.perf_counter()


def mark_app_constructed() -> None:
    """Record that the TUI object exists and the framework is about to boot.

    Paired with :func:`mark_first_frame` to bracket the framework's own startup
    — terminal setup, first layout, first paint — as ``tui-boot``. Two marks
    rather than a ``with`` block because the region spans a return out of one
    function and into a framework callback, which no context manager can hold.

    Also closes ``client-prep:app-construct`` (P4, paired with
    :func:`mark_tui_import_done`) — #3671's original single ``client-prep``
    lump (``mark_app_constructed`` minus ``mark_async_entered``) is BROKEN
    DOWN into 4 named sub-stages at the seams architect's design identified
    (``client-prep:transport`` / ``:read-model`` in ``repl.py``,
    ``:tui-import`` around the lazy import, ``:app-construct`` here).

    #3735 regression, caught by the owner's own real-machine re-measurement
    (``unaccounted`` 6.9% → 62%) before it could ship a second time: the 4
    named sub-stages are NARROW brackets — each covers one specific call, not
    the control-flow BETWEEN them (registry/session setup between
    ``mark_async_entered`` and ``client-prep:transport``, ``resolve_render_mode``
    between ``:read-model`` and ``:tui-import``, …). The wide bracket the 4
    sub-stages replaced covered that space; narrowing to 4 named points
    without also capturing what falls between them silently moved that time
    from "measured, at a coarser granularity" to gone. Fixed by keeping the
    wide bracket (``_ASYNC_ENTERED_AT`` → this mark) as the ground truth and
    recording whatever the 4 named sub-stages do NOT explain of it as
    ``client-prep:other`` — a full breakdown of the SAME span the old single
    lump covered, not a replacement of it. The named sub-stages' sum plus
    ``client-prep:other`` therefore always equals this wide span exactly, by
    construction — never spills into the process-wide ``unaccounted``, which
    stays reserved for the genuinely-unbracketed stages (``config`` /
    ``registry`` / ``session`` / …) outside this function's own concern.
    """
    global _APP_CONSTRUCTED_AT
    if _APP_CONSTRUCTED_AT is None:
        _APP_CONSTRUCTED_AT = time.perf_counter()
        if _TUI_IMPORT_DONE_AT is not None:
            TIMING.record(
                "client-prep:app-construct", _APP_CONSTRUCTED_AT - _TUI_IMPORT_DONE_AT
            )
            # Nested under the SAME `_TUI_IMPORT_DONE_AT is not None` guard as
            # `client-prep:app-construct` above (not a sibling `if
            # _ASYNC_ENTERED_AT is not None`, which a full-suite run caught
            # failing: `_ASYNC_ENTERED_AT` is a module-wide singleton, so an
            # EARLIER unrelated test/run leaves it set long after this one's
            # own `_TUI_IMPORT_DONE_AT` was reset to `None` — computing
            # `client-prep:other` from a stale `_ASYNC_ENTERED_AT` in that
            # case is exactly the "bogus span against a stale/absent mark"
            # `test_app_constructed_without_a_prior_tui_import_mark_records_
            # nothing` already guards against for `:app-construct`). If the
            # TUI-import sub-stage never ran, the whole client-prep sub-stage
            # sequence is undefined, and no residual should be computed
            # either.
            if _ASYNC_ENTERED_AT is not None:
                wide = _APP_CONSTRUCTED_AT - _ASYNC_ENTERED_AT
                named = sum(TIMING.elapsed(s) for s in _CLIENT_PREP_NAMED_STAGES)
                TIMING.record("client-prep:other", max(0.0, wide - named))


def mark_first_frame() -> None:
    """Record that the interface is now on screen.

    Idempotent: only the FIRST call counts. A surface that re-mounts (a session
    switch, a resize that rebuilds the app) must not move the end of startup to
    a later moment and shrink every share that came before it.
    """
    global _FIRST_FRAME_AT
    if _FIRST_FRAME_AT is None:
        _FIRST_FRAME_AT = time.perf_counter()
        if _APP_CONSTRUCTED_AT is not None:
            TIMING.record("tui-boot", _FIRST_FRAME_AT - _APP_CONSTRUCTED_AT)


def first_frame_reached() -> bool:
    """Whether the interface actually appeared.

    #3671 follow-up: the report must be able to tell "startup completed" from
    "startup was interrupted/crashed before the interface appeared" — see
    :func:`process_elapsed_seconds`'s docstring for why conflating the two
    fooled 3 measurements in a row on real data before this existed.
    """
    return _FIRST_FRAME_AT is not None


def process_elapsed_seconds() -> float:
    """Seconds from import to the interface appearing, OR to now.

    Falls back to "now" when the interface never appeared — a startup that
    failed or was interrupted still has a wall time worth reporting. This
    value alone cannot distinguish the two cases: call :func:`first_frame_
    reached` and label the number accordingly before showing it to anyone
    (``report_lines`` does this). A caller that shows this number under a
    "TOTAL (start -> interface on screen)" label without checking
    ``first_frame_reached()`` first is asserting something that may be
    false — measured doing exactly that: a `reyn chat` interrupted mid-
    `import litellm`, well before the interface existed, printed a
    plausible-looking ``TOTAL 3.02s`` with EVERY client-prep stage reading
    0.00s and no indication anything was wrong beyond a >20% ``unaccounted``
    warning that reads identically whether the interface appeared slowly or
    never appeared at all.
    """
    end = _FIRST_FRAME_AT if _FIRST_FRAME_AT is not None else time.perf_counter()
    return end - _MODULE_IMPORTED_AT

#: The startup stages, in the order they run. Declared here rather than
#: collected as they report, so the output has a stable shape an operator can
#: read the same way twice — and so a stage that did not run is visible as
#: ``0.00`` rather than as a missing line.
#:
#: ``first-frame`` is deliberately NOT here. It named the ENDPOINT of startup,
#: not an interval, so nothing ever recorded a duration against it and it
#: printed ``0.00`` on every machine — including one taking 7.58s to reach the
#: interface, where it read as "the TUI appears instantly". A row that cannot
#: hold a value is worse than no row: this module's whole premise is that
#: ``0.00`` means measured-and-fast, and one entry quietly breaking that
#: promise poisons every other reading. The span it stood for is ``TOTAL``.
#: #3671: ``client-prep:litellm-import`` — REMOVED, not left to report
#: ``0.00`` (this module's own stated line above: "``0.00`` means
#: measured-and-fast", not "this stage no longer exists"; a permanently-dead
#: row would break that promise the same way a never-fired ``first-frame``
#: did). Removed once the forced startup-time ``import litellm`` it measured
#: was itself removed (``reyn.llm.litellm_bootstrap`` module docstring) — a
#: session that never calls the LLM no longer has a litellm-import cost
#: anywhere in this bracket to name.
STAGES: "tuple[str, ...]" = (
    "import",
    "config",
    "registry",
    "plugins",
    "mcp",
    "session",
    "client-prep:transport",
    "client-prep:read-model",
    "client-prep:tui-import",
    "client-prep:app-construct",
    "client-prep:other",
    "tui-boot",
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

    def elapsed(self, stage: str) -> float:
        """Seconds recorded for *stage*, or ``0.0`` if it never ran.

        The public read side of :meth:`record` — lets a stage that is itself
        DERIVED from others (``client-prep:other``, #3735) sum its siblings
        without reaching into ``_elapsed`` directly.
        """
        return self._elapsed.get(stage, 0.0)

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

    def report_lines(
        self, wall_seconds: float, *, first_frame_reached: bool,
    ) -> "list[str]":
        """The report, as short lines meant to be read off a screen.

        One stage per line with its share, then the total and whatever is left
        over. Shares are of WALL time rather than of the measured sum, so a
        stage reading 5% of a startup dominated by something unmeasured cannot
        be misread as 5% of the problem.

        ``first_frame_reached`` (#3671 follow-up) is a REQUIRED keyword, no
        default: a startup that was interrupted or crashed before the
        interface appeared gets a report that SAYS SO, prominently, instead
        of a ``TOTAL`` line indistinguishable from a completed one. Measured
        doing the wrong thing 3 times in a row before this flag existed:
        `process_elapsed_seconds()`'s "now" fallback produces a real,
        plausible-looking number, and the old unconditional
        ``TOTAL ... (start \u2192 interface on screen)`` label asserted the
        interface appeared regardless of whether it actually did. A default
        of ``True`` would hand that same false claim to any caller who
        forgot to pass it \u2014 the exact bug this parameter exists to close.
        """
        lines = ["startup timing (REYN_STARTUP_TIMING=1)"]
        if not first_frame_reached:
            lines.append(
                "  \u26a0 interface NEVER appeared \u2014 this is NOT a completed "
                f"startup. {wall_seconds:.2f}s elapsed before the process exited "
                "or was interrupted; the breakdown below is whatever ran in "
                "that window, not a full startup's worth of stages."
            )
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
        if first_frame_reached:
            lines.append(
                f"  {'TOTAL':<12} {wall_seconds:6.2f}s  (start \u2192 interface on screen)"
            )
        else:
            lines.append(
                f"  {'ELAPSED':<12} {wall_seconds:6.2f}s  (start \u2192 exit; NOT a "
                "TOTAL \u2014 see the warning above)"
            )
        # #3735: a stage breakdown that mostly reads `unaccounted` is the same
        # failure this module exists to prevent, just one level up \u2014 the
        # instrument itself must say so rather than let a large gap masquerade
        # as a tidy-looking report. Self-flagged here (not asserted/raised):
        # this is a diagnostic, and a diagnostic that crashes the startup it
        # is trying to explain is worse than the problem it reports.
        if first_frame_reached and share >= _UNACCOUNTED_GAP_WARNING_PCT:
            lines.append(
                f"  \u26a0 unaccounted is {share:.0f}% of TOTAL \u2014 the stage "
                "brackets likely have a coverage gap, not just untimed work"
            )
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
