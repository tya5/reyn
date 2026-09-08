"""Event-loop responsiveness tripwire — the runtime-generic core (#3539,
#5870; lifted out of the inline CUI by #5898).

Two layers, because they answer different questions and only one of them can be
switched on in advance.

**A tripwire that is always on.** The symptom this exists for — "the UI froze
while a reply streamed" (#3539), "no HTTP request returned for 7 minutes"
(#5898) — arrives unannounced, so an opt-in probe is only ever enabled *after*
someone has already lost the occurrence they wanted to measure. #3638 closed
exactly that way: by the time anyone could look, the symptom had stopped
happening. The tripwire therefore runs unconditionally and costs a comparison
against a float per tick; when the loop is late by more than
:data:`_TRIPWIRE_MS` it says so ONCE, and says what to do next.

**Detail behind an env var.** Everything that costs more than a comparison —
per-chunk wait/work split, per-delta handler timing — is written only when
``REYN_PROF_DUMP`` names a file. Naming a path rather than taking a boolean
matches ``REYN_LLM_TRACE_DUMP``, the instrumentation idiom already in the tree,
and it makes "where did it go" answerable without reading this module.

**Environment axes travel with the numbers.** A measurement that cannot be
compared to another measurement is a number, not evidence. #3539 stalled at
"the condition is unidentified" precisely because the owner's environment and
the measuring environment differed along axes nobody had written down: which
provider, which terminal size, how many sessions at once. Every record here
carries those, so the next capture can be held against this one.

**Why this lives in ``runtime`` (#5898).** ``reyn:web``'s loop blocked for 7
minutes on O(history bytes) CPU work (#5894 ②) and, with the shipped config,
that stall was visible ONLY as its clients' symptoms: ``stall_trace.py`` is a
``REYN_STALL_TRACE`` opt-in, and the tripwire lived in ``textual_chat`` — a
server had no watcher at all. The tripwire, its durable record, and the
per-tick ``faulthandler`` dead-man's switch (:class:`StallDumpArm` — the
#5877/#5873 fd-lifecycle rules, ONE implementation) now live here, and
:func:`watch_event_loop` is the ONE tick loop both the CUI
(``TextualChatApp._watch_loop_responsiveness``, which adds its own chrome and
pump/keys context) and ``reyn:web``'s lifespan run. ``reyn.interfaces.inline.
textual_chat.loop_probe`` re-exports every name below for the CUI's own use.

Measured baseline this was built against (real TUI, real model, 463 chunks):
work 0.31 ms/chunk, wait 17.84 ms/chunk, **0** loop stalls over 12 ms. The
tripwire is silent on a healthy loop by construction, not by tuning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot, diagnostic_snapshot

#: A loop tick later than this is worth telling someone about. Set well above
#: the measured healthy ceiling (a 10 ms-period task never exceeded 12 ms over
#: 463 chunks) so an ordinary stream never trips it — the tripwire's value is
#: that it stays quiet, and a threshold that fires on healthy runs would be
#: read as noise and ignored.
_TRIPWIRE_MS = 250.0

#: How often the tripwire wakes. Long enough to cost nothing, short enough that
#: a stall a human would notice cannot hide between two ticks.
_TICK_SECONDS = 0.05

#: Minimum gap between two durable ``write_record("tripwire", ...)`` calls for
#: the SAME ongoing stall (#4761 ①). Deliberately independent of the
#: once-only banner/log notice below — that silence is about not burying a
#: human-facing reply; this one is about the durable record still being able
#: to answer "did it recover, or keep getting worse?" while nobody is
#: watching, which is exactly the question a frozen screen cannot answer on
#: its own. 2s: short enough that a multi-second stall (#4761's own report)
#: leaves several data points, long enough that a multi-tick stall spanning
#: seconds doesn't write one record per :data:`_TICK_SECONDS` (20/s) and
#: flood ``REYN_PROF_DUMP`` — no config knob added for this one, since it
#: gates a file that is already opt-in behind ``REYN_PROF_DUMP`` itself.
_RECORD_INTERVAL_S = 2.0

_DUMP_ENV = "REYN_PROF_DUMP"


def stall_dump_path(reyn_log_path: "str | None") -> "str | None":
    """The fixed, single-file destination for a stall's stack dump (#5977
    ruling ②, #5978 ①'s ``diagnostic_snapshot`` shape) — the SAME
    directory ``reyn.log`` lives in, so an operator who already knows to
    look there finds it, under a FIXED filename: no config surface at
    all ("a limit that can be set is a limit someone can raise" —
    architect, #5977).

    Overwritten in place rather than rotated: a dump answers "what was
    stuck at the moment of the LAST stall," and a LATER stall's dump is
    never a worse sample than an earlier one it replaces — there is no
    reason to keep generations (architect's own self-correction, #5977:
    an earlier ruling proposed rotation here, which #5977 itself exists
    to call out — "a bound written for a mechanism this doesn't have").

    ``None`` when there is no ``reyn.log`` path to sit beside — matches
    :meth:`StallDumpArm.open`'s own "no ``FileHandler`` installed → never
    arms" behaviour."""
    if reyn_log_path is None:
        return None
    return str(Path(reyn_log_path).with_name("stall_dump.log"))


def dump_path() -> "str | None":
    """The detail-probe output path, or ``None`` when detail is off.

    Read at call time rather than captured at import, so a long-lived session
    can be told to start recording without a restart — the same reason
    ``REYN_LLM_TRACE_DUMP`` is read per call.
    """
    return os.environ.get(_DUMP_ENV) or None


def environment_axes() -> "dict[str, Any]":
    """The axes a later measurement has to match on to be comparable.

    Not diagnostics for their own sake: #3539 could not be settled because the
    owner's environment and the measuring one differed along axes nobody had
    recorded, so two sets of numbers could not be held against each other. Each
    field here is one of those axes, and each is read defensively — a probe that
    raises while collecting context would destroy the occurrence it exists to
    capture.

    #5870 stage 1 adds ``loadavg``/``process_footprint_bytes``: the owner's
    own report ("2.5s とか発生してるんだけど...idle でも出る") named a stall
    that arrives with NO conversation activity, which the code-path census
    this issue also ran could not explain on its own — leaving exactly two
    live hypotheses this module cannot tell apart without its own numbers:
    (a) reyn's own code blocking the loop (the stack dump alongside this
    record answers that one directly), or (b) the HOST failing to schedule
    the process at all (a saturated CPU run queue, or the swap pressure the
    owner's own machine hit the same night, #5851) — the process's own loop
    could be perfectly idle and still starved. ``os.getloadavg()``'s 1-minute
    figure is the cheapest live signal for exactly that: a run queue longer
    than the core count says the host was oversubscribed AT THIS INSTANT,
    independent of whether reyn's own code did anything wrong. Paired with
    the process's own current memory footprint (:mod:`reyn.runtime.
    process_memory`, #5851/#5858 — the SAME reader ``ProcessMemoryGuard``
    uses, ~39µs, no fork/subprocess) so a swap-pressure host stall and a
    healthy one are distinguishable from the SAME record a stall wrote,
    without a second, separately-timed capture.
    """
    axes: "dict[str, Any]" = {}
    try:
        import platform

        axes["platform"] = platform.platform()
        axes["python"] = platform.python_version()
    except Exception:  # noqa: BLE001 - context is never worth an exception
        pass
    try:
        import textual_flowview

        axes["flowview"] = getattr(textual_flowview, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        pass
    for var in ("TERM", "TERM_PROGRAM", "COLUMNS", "LINES"):
        value = os.environ.get(var)
        if value:
            axes[var.lower()] = value
    try:
        # POSIX only (AttributeError on Windows) — matches this function's
        # own "never fabricate a value this platform cannot produce" rule
        # for the platform-guarded fields below.
        axes["loadavg_1m"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    try:
        from reyn.runtime.process_memory import (
            make_process_memory_reader,
            process_memory_metric_name,
        )

        _footprint_metric = process_memory_metric_name()
        if _footprint_metric is not None:
            _footprint_bytes = make_process_memory_reader()()
            if _footprint_bytes is not None:
                axes["process_footprint_bytes"] = _footprint_bytes
                axes["process_footprint_metric"] = _footprint_metric
    except Exception:  # noqa: BLE001
        pass
    return axes


def write_record(kind: str, **fields: Any) -> None:
    """Append one detail record, with the environment axes attached.

    No-op when detail is off. Best-effort by construction: an instrument that
    can break the thing it measures is worse than no instrument.
    """
    path = dump_path()
    if not path:
        return
    try:
        record = {"kind": kind, "ts": time.time(), **fields, "env": environment_axes()}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class LoopTripwire:
    """Watches how late the event loop runs, and speaks once when it is late.

    Holds the maximum lateness seen so it survives past the tick that saw it —
    a stall reported as "it happened" with no magnitude cannot be compared to
    anything, which is the failure this whole module is a response to.
    """

    def __init__(self, *, threshold_ms: float = _TRIPWIRE_MS) -> None:
        self._threshold_ms = threshold_ms
        self._max_lateness_ms = 0.0
        self._fired = False
        #: #5977 ①: has the CURRENT (still-ongoing) episode already produced
        #: a stack dump — reset alongside ``_fired`` at recovery, below,
        #: since both answer "has THIS episode already told its one story."
        #: No SESSION-total cap alongside this one (dropped, #5977 ②):
        #: once the dump moved to its own always-overwritten file
        #: (:func:`stall_dump_path`), the reason a session cap existed —
        #: an unbounded run of dumps pushing OLDER OPERATIONAL ``reyn.log``
        #: lines out of the retained window — no longer applies (nothing
        #: is pushed out of anything; a NEW dump simply replaces the OLD
        #: one). A cap would instead have frozen the file at whichever
        #: stall happened to be the Nth, silently hiding every LATER one —
        #: the exact "bound outlives the reason it was written for" shape
        #: #5973 named. The per-episode gate above is what still answers
        #: the band's "who stops this if it repeats" question: it bounds
        #: the SELF-AMPLIFICATION within one stall (each dump's own
        #: synchronous write cost adding to the very lateness that risked
        #: triggering the next one), which is unrelated to file growth.
        self._dumped_this_episode = False
        #: Wall-clock time of the last durable ``write_record`` call, or
        #: ``None`` before the first one — independent of ``_fired`` (#4761
        #: ①: one flag was gating two different questions).
        self._last_recorded_monotonic: "float | None" = None
        #: Whether the most recent tick was above threshold — lets a healthy
        #: tick tell "recovered" (this was ``True``) from "was already
        #: healthy" (this was ``False``) apart, so the durable trace can say
        #: which one happened instead of just stopping either way.
        self._in_stall = False
        #: One-shot recovery flag for :meth:`consume_recovered` — separate
        #: from the ``write_record`` call at the same transition (architect
        #: finding, #4797 follow-up): ``write_record`` is a no-op on the
        #: shipped default (``REYN_PROF_DUMP`` unset), so recovery was only
        #: ever visible to an already-armed session. This lets the CALLER
        #: (the tick loop) put a recovery notice on a surface that needs no
        #: prior setup — the same "visible with the shipped config" bar the
        #: once-only stall notice already clears.
        self._just_recovered = False
        #: #4855: whether the CURRENT (still-ongoing) stall episode's onset
        #: was itself returned to the caller — i.e. whether ``observe()``'s
        #: ``self._fired`` gate let this specific episode's stall notice
        #: through, as opposed to being silently swallowed by an EARLIER
        #: episode having already consumed the session's one-shot notice.
        #: Set True only at the exact point ``observe()`` returns a stall
        #: magnitude; read (and reset) at the matching recovery transition.
        #: Without this, ``_just_recovered`` fired on EVERY recovery
        #: regardless of whether its own episode's onset was ever told to
        #: anyone — the user-facing text is literally "recovered from the
        #: stall reported above," and a session whose FIRST stall happened
        #: during app-mount startup jitter (consuming ``_fired``) could
        #: recover from every SUBSEQUENT episode with no "reported above"
        #: to point at (#4855's root cause).
        self._current_stall_reported = False

    @property
    def threshold_ms(self) -> float:
        """The lateness above which a tick counts as a stall — the ONE
        deadline :func:`watch_event_loop`'s dead-man's switch and
        :meth:`observe`'s own comparison both read (#5870: "did the loop
        stall" and "should a stack have been dumped for it" can never
        disagree about where the line is)."""
        return self._threshold_ms

    @property
    def max_lateness_ms(self) -> float:
        """The worst lateness observed so far, in milliseconds."""
        return self._max_lateness_ms

    @property
    def fired(self) -> bool:
        """Whether the CURRENT stall episode's onset has already been
        reported (#4855: reset at each recovery, not "ever, this
        session" — see :meth:`observe`'s recovery branch for why)."""
        return self._fired

    def should_arm_stack_dump(self) -> bool:
        """Whether the caller's dead-man's switch (:class:`StallDumpArm`)
        should be RE-ARMED this tick (#5977 ①).

        ``False`` once the CURRENT episode already produced a dump
        (:meth:`record_stack_dump` set that — recovery below clears it for
        the NEXT episode). Consulted BEFORE ``StallDumpArm.rearm()`` —
        there is no separate ``faulthandler`` state to cancel; skipping
        the re-arm IS the suppression, since each arm is one-shot and
        replaces the previous pending timer (see :class:`StallDumpArm`'s
        own docstring)."""
        return not self._dumped_this_episode

    def record_stack_dump(self) -> None:
        """Report that an armed dump actually fired this tick (the caller's
        own ``stack_dumped`` proxy turned ``True``) — closes this episode's
        one-shot allowance; the caller (:func:`watch_event_loop`) also
        truncates the dump file's fd for the NEXT episode right after
        calling this, via ``StallDumpArm.mark_fired()`` — see that
        method's own docstring for why that must happen AFTER, not on
        every re-arm."""
        self._dumped_this_episode = True

    def consume_recovered(self) -> bool:
        """Whether the loop just recovered from a stall — ``True`` at most
        once per episode, consumed on read (mirrors :meth:`observe`'s own
        once-only return for the stall side) so a caller logging this on a
        default-visible surface doesn't repeat it either."""
        recovered = self._just_recovered
        self._just_recovered = False
        return recovered

    def observe(
        self,
        lateness_ms: float,
        *,
        pump_ticks: "int | None" = None,
        turn_active: "bool | None" = None,
        stack_dumped: "bool | None" = None,
    ) -> "float | None":
        """Record one tick's lateness; return it the FIRST time it is bad.

        Returns ``None`` on every later crossing as well as on healthy ticks: a
        freeze is one event to a person watching it, and repeating the notice
        per tick would bury the reply the notice is about.

        Returns the magnitude rather than a sentence because the two surfaces
        that report it need different lengths (:func:`stall_banner` for the one
        always-visible chrome row, :func:`stall_log_line` for the durable
        record) — wording either one here would make this the place a caller
        has to work around.

        #4761 ①: the once-only rule above governs the RETURN VALUE (what the
        human-facing banner/log notice does) — it does not also govern the
        internal :func:`write_record` call. Those answer different questions:
        the notice is "tell someone now, once," the durable record is "can a
        later reader tell whether this recovered or kept getting worse,"
        which silence cannot answer either way. So ``write_record`` keeps
        firing at :data:`_RECORD_INTERVAL_S` while ``lateness_ms`` stays
        above threshold, independently of whether this call also returns a
        value — AND a healthy tick that follows a stall writes one
        ``"tripwire_recovered"`` record, for the same reason: a trace that
        just stops leaves "it recovered" and "the process died mid-stall"
        looking identical, the same silence-hides-two-states shape #4761's
        original defect had, one level up.

        ``pump_ticks``, if the caller has one (#4761 ②: ``TextualChatApp``'s
        own message-pump heartbeat counter), rides along in every
        ``write_record`` call this method makes — a *comparable* value
        across the periodic ``"tripwire"`` records lets an armed session
        see whether the count kept moving DURING an ongoing, not-yet-
        recovered stall, which the once-per-episode default-visible notice
        (below) cannot show on its own.

        ``turn_active``, if the caller has one (#4761: whether a turn was
        running the instant this tick was observed — see
        :func:`stall_log_line`'s own docstring for why this matters), rides
        along the same way for the ARMED trace's own record — this method's
        own default-visible RETURN VALUE is what :func:`stall_log_line`
        actually surfaces to an unarmed session; this ``write_record`` call
        only adds it to the opt-in detail dump for consistency with
        ``pump_ticks``.

        ``stack_dumped`` (#5870 stage 1): whether the caller's own
        ``reyn.runtime.stall_trace`` re-arm (a per-tick dead-man's switch,
        see that module's own updated docstring) actually fired for THIS
        tick and left a stack dump in ``reyn.log`` — best-effort, not a
        byte-exact readback of ``faulthandler``'s own internal state (it
        has none to read), so the caller derives it from the SAME
        ``lateness_ms > threshold`` comparison this method already makes
        internally to flag a stall. Riding here rather than in a second
        call keeps "was there a stall" and "is there a stack for it"
        answerable from the ONE record a later reader opens.
        """
        if lateness_ms > self._max_lateness_ms:
            self._max_lateness_ms = lateness_ms
        extra: "dict[str, Any]" = {}
        if pump_ticks is not None:
            extra["pump_ticks"] = pump_ticks
        if turn_active is not None:
            extra["turn_active"] = turn_active
        if stack_dumped is not None:
            extra["stack_dumped"] = stack_dumped
        if lateness_ms <= self._threshold_ms:
            if self._in_stall:
                self._in_stall = False
                # #4855: report a recovery only for the episode whose OWN
                # onset was told to the caller — write_record stays
                # unconditional (#4761 ①'s durable-record guarantee is
                # untouched), but _just_recovered must not fire for an
                # episode that was silently swallowed below because an
                # earlier episode already consumed the one-shot notice.
                if self._current_stall_reported:
                    self._just_recovered = True
                    self._current_stall_reported = False
                # lead-coder ruling, #4855: reset the one-shot notice gate
                # HERE, at recovery — not "once per App session" but "once
                # per un-recovered episode." Without this, an early stall
                # (e.g. app-mount startup jitter) permanently consumed the
                # session's only notice, and every LATER, possibly far more
                # serious freeze went unreported for the rest of the
                # session — the exact hole #4855 measured. The original
                # reason for "once, not per-tick" (a notice repeated per
                # tick buries the reply it's about) is unaffected: this
                # still reports at most once PER STALL, only the boundary
                # between stalls moved from "session start" to "the
                # previous stall's own recovery."
                self._fired = False
                # #5977 ①: the NEXT episode gets its own fresh one-shot dump
                # allowance — the cap is a SESSION total (untouched here),
                # not a per-episode one, so only this flag resets.
                self._dumped_this_episode = False
                write_record(
                    "tripwire_recovered", lateness_ms=round(lateness_ms, 1), **extra,
                )
            return None
        self._in_stall = True
        now = time.monotonic()
        if (
            self._last_recorded_monotonic is None
            or now - self._last_recorded_monotonic >= _RECORD_INTERVAL_S
        ):
            write_record("tripwire", lateness_ms=round(lateness_ms, 1), **extra)
            self._last_recorded_monotonic = now
        if self._fired:
            return None
        self._fired = True
        self._current_stall_reported = True
        return lateness_ms


def stall_banner(lateness_ms: float) -> str:
    """The status-line segment for a stall — short, because it shares the ONE
    always-visible chrome row with ``model │ agent │ cost │ ctx`` and a
    narrow terminal has no room to spare.

    Deliberately plain text, no glyph, for the reason ``chrome.status_line_text``
    records: every other character on that row is 1 terminal cell wide, and a
    1-cell misjudgement breaks the whole row rather than just this segment.
    """
    return f"unresponsive {lateness_ms / 1000:.1f}s"


def stall_log_line(
    lateness_ms: float,
    *,
    pump_ticks: "int | None" = None,
    pump_delta: "int | None" = None,
    pump_window_s: "float | None" = None,
    keys_received: "int | None" = None,
    keys_delta: "int | None" = None,
    turn_active: "bool | None" = None,
) -> str:
    """The durable record of a stall — the one that survives the operator
    looking away.

    The status-line segment is what makes the stall noticeable at the moment
    it happens; a stall is noticed by whoever is watching, and the person
    diagnosing it later is usually not that person. This line carries the
    magnitude AND how to record the detail on the next occurrence, which the
    short segment has no room for.

    ``pump_ticks``/``pump_delta``/``pump_window_s`` (#4761 ②, lead-coder
    review): a FIRST design compared this line's ``pump_ticks`` reading
    against a SECOND one in :func:`stall_recovered_log_line`, at recovery.
    That pair never completes for a freeze that never recovers — the exact
    shape of #4761's own report (the operator killed the process; no
    recovery line was ever going to fire) — which is precisely the case
    where knowing whether the pump was still moving matters most.
    ``pump_delta``, when given, is how much :attr:`TextualChatApp.
    pump_ticks` changed over the trailing ``pump_window_s`` seconds
    BEFORE this notice fired — self-contained in ONE line, no second
    event required. ``0`` here is the H1 signal (pump had already
    stopped advancing before the stall was even noticed); a positive
    delta rules H1 out for this episode on its own.

    ``keys_received``/``keys_delta`` (#4761 ③) follow the SAME
    self-contained-in-one-line shape, deliberately not a pair with a
    recovery-side reading, for the same reason: an H3 diagnosis ("keys
    aren't reaching the App at all") is needed most on a freeze that never
    resolves. A ticking pump (``pump_delta`` > 0) with ``keys_delta`` at
    ``0`` despite an operator who reports pressing several keys is H3, not
    H1/H2 — the pump is fine, input simply never arrived.

    ``turn_active`` (#4761, architect's outstanding point, still unimplemented
    when ①②③ landed): whether a turn was running AT THE MOMENT this notice
    fired — the ①②③ trio (tripwire lateness, pump heartbeat, key-arrival
    count) all discriminate BETWEEN hypotheses for what stopped the interface,
    but only make sense if the interface was actually being asked to do
    something. Without this, a byte-identical, unchanging screen is not
    evidence of a freeze on its own — it is equally consistent with "nothing
    was happening" (no turn in flight, an idle screen that simply has nothing
    to redraw). Passes the same two design questions ①②③ already had to pass
    (lead-coder, tonight, three times): visible with shipped defaults —
    riding this line's own ``logger.warning`` call, the same always-on
    surface ①②③'s own notices use, NOT a second ``write_record`` call that
    would need ``REYN_PROF_DUMP`` armed in advance; and who binds it — the
    caller (:meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
    _watch_loop_responsiveness`) reads its own ``ActivityRow.state`` (already
    the app's existing "is a turn running" surface — see ``turn_active=`` at
    the compact-caps call site) at the same instant it reads ``pump_ticks``.
    """
    ticks_note = ""
    if pump_ticks is not None:
        if pump_delta is not None and pump_window_s is not None:
            ticks_note = (
                f" (pump heartbeat at {pump_ticks}, +{pump_delta} in the "
                f"last {pump_window_s:.0f}s)"
            )
        else:
            ticks_note = f" (pump heartbeat at {pump_ticks})"
    keys_note = ""
    if keys_received is not None:
        if keys_delta is not None and pump_window_s is not None:
            keys_note = (
                f" (keys received: {keys_received}, +{keys_delta} in the "
                f"last {pump_window_s:.0f}s)"
            )
        else:
            keys_note = f" (keys received: {keys_received})"
    turn_note = ""
    if turn_active is not None:
        turn_note = f" (turn {'active' if turn_active else 'idle'} at the time)"
    return (
        f"the interface was unresponsive for {lateness_ms / 1000:.1f}s"
        f"{ticks_note}{keys_note}{turn_note} — re-run with {_DUMP_ENV}=<path> "
        "to record what it was doing"
    )


def stall_recovered_log_line(*, pump_ticks: "int | None" = None) -> str:
    """The default-visible recovery notice — no ``REYN_PROF_DUMP`` required.

    architect finding (#4797 follow-up): every OTHER new signal this module
    gained (the repeated ``"tripwire"`` record, ``"tripwire_recovered"``)
    goes through :func:`write_record`, a no-op on the shipped default. A
    session that never armed ``REYN_PROF_DUMP`` before a stall — the exact
    situation #4761's own report was in — got nothing new from that work.
    This line is deliberately NOT gated on the env var, so "it fired, then
    it recovered" is readable from an ordinary, unarmed run's own logs.

    The caller (``app.py``'s ``_watch_loop_responsiveness``) logs this at
    ``logger.warning`` — same severity as the stall notice, not
    ``.info``. An initial ``.info`` ruling was self-caught and reverted
    before landing: the interactive CUI's own ``_setup_interactive_logging``
    sets the ROOT logger's level to WARNING, so an INFO record from a
    logger with no override of its own never reaches the file at all —
    not quieter, genuinely absent. Stall and recovery are the start and
    end of ONE episode; one WARNING line per episode is not a second
    alarm.

    ``pump_ticks`` (#4761 ②), when given, is the App's own message-pump
    heartbeat counter's value at recovery — held against
    :func:`stall_log_line`'s own reading from the SAME episode's start.
    If the two differ, the pump kept dispatching messages throughout the
    stall (H1 — "the pump stopped" — is ruled out for this episode); if
    they are equal, the pump was frozen for the whole stall.
    """
    ticks_note = f" (pump heartbeat at {pump_ticks})" if pump_ticks is not None else ""
    return f"the interface recovered from the stall reported above{ticks_note}"


class StallDumpArm:
    """The per-tick ``faulthandler`` dead-man's switch — the ONE
    implementation of the #5877/#5873 fd-lifecycle rules AND #5977 ②'s
    single-overwritten-file rule (#5898: lifted out of
    ``TextualChatApp._watch_loop_responsiveness`` so ``reyn:web``'s
    tripwire dumps the same way, not a second copy of the same hazards).

    **Why its own dedicated file, not ``reyn.log`` (#5977 ②)**: dump
    volume measured on the owner's own machine was 49% of their entire
    ``reyn.log`` (8,832 of 17,958 lines) — pushing OLDER OPERATIONAL log
    lines (potentially a real ``Fatal Python error`` crash record) out of
    the retained rotation window before an operator could read them. A
    dump answers ONE question — "what was stuck at the moment of the
    LAST stall" — and a later stall's dump is never a worse sample than
    an earlier one it replaces, so keeping generations has no purpose;
    the file is a single always-overwritten :func:`~reyn.runtime.
    diagnostic_snapshot.diagnostic_snapshot` (#5978 ①'s general shape,
    stall dump as its first caller). Boundedness comes from the SHAPE —
    always exactly one dump, ~14.6 KB measured — not from a config knob:
    "a limit that can be set is a limit someone can raise" (architect).

    **Why an fd of its own (#5877, architect ruling, real-machine
    measurement)**: ``faulthandler.dump_traceback_later`` captures the
    ``file`` argument's underlying FILE-DESCRIPTOR NUMBER at arm time, not
    a live object. A caller that stays armed across MANY of its own calls
    must therefore arm against an fd nothing else can close and reuse —
    :class:`~reyn.runtime.diagnostic_snapshot.DiagnosticSnapshot` opens
    and holds exactly one, for this arm's whole lifetime. **No log path
    installed means :meth:`open` returns ``None`` and nothing ever
    arms** — deliberately, not a fail-open: a dump with no genuinely
    stable destination was never a safe thing to attempt.

    **The truncate-timing trap (architect, #5977)**: "arm every tick,
    truncate every re-arm" is WRONG — ``dump_traceback_later`` commits to
    the fd NUMBER at arm time, and a re-arm happens every
    :data:`_TICK_SECONDS` (50 ms), so truncating on every re-arm would
    erase a PENDING, not-yet-fired dump before it ever gets to write.
    :meth:`mark_fired` — truncate + reopen for the NEXT episode — must be
    called only AFTER the caller has OBSERVED a fire (the ``stack_dumped``
    proxy turned ``True``), never on an ordinary re-arm.

    **Why ``repeat=False`` and re-arm every tick**: each re-arm cancels and
    replaces the PENDING one-shot timer, so it only ever actually FIRES
    when a tick fails to arrive in time to re-arm it — i.e. while the very
    thing it watches is blocked (see ``stall_trace.arm``'s own docstring).

    **Why every re-arm re-verifies the fd (lead-coder BLOCKING, PR #5988
    review)**: losing the rotation MECHANISM (② moved the dump off
    ``reyn.log``, which was the only thing ever rotating it) does not
    remove the CLASS of problem — an armed fd can still end up pointing
    at a file nothing else reads: an external cleanup tool touching
    ``stall_dump.log``, an operator ``rm``-ing it, anything that changes
    what's AT that path without this arm knowing. A write against such an
    orphaned fd still SUCCEEDS — silently — so the operator sees an empty
    or missing file at the path they know to check and reads it as "no
    stall happened," not "the dump went somewhere I can't see." Each
    :meth:`rearm` therefore asks :meth:`~reyn.runtime.diagnostic_snapshot.
    DiagnosticSnapshot.points_at_current_file` — inode identity, cut on
    the file identity changing, not a clock, exactly the question a
    rotation-survival check would ask, generalized to any cause — and
    reopens (:meth:`~reyn.runtime.diagnostic_snapshot.DiagnosticSnapshot.
    reset`) on a mismatch before arming.
    """

    def __init__(
        self, *, seconds: float, snapshot: "DiagnosticSnapshot", logger: logging.Logger, label: str,
    ) -> None:
        self._seconds = seconds
        self._snapshot = snapshot
        self._logger = logger
        self._label = label

    @classmethod
    def open(
        cls, *, seconds: float, path: "str | None", logger: logging.Logger, label: str,
    ) -> "StallDumpArm | None":
        """Open the dump destination once; ``None`` when there is no
        *path* (no ``FileHandler`` installed to derive one from — see
        :func:`stall_dump_path`) or it cannot be opened — in both cases
        the dead-man's switch simply never arms for this watcher."""
        if path is None:
            return None
        snapshot = diagnostic_snapshot(path)
        if snapshot is None:
            logger.error("%s: could not open the tripwire's own stall-dump snapshot at %s", label, path)
            return None
        return cls(seconds=seconds, snapshot=snapshot, logger=logger, label=label)

    @property
    def armed(self) -> bool:
        """Whether this arm currently holds a usable fd (False after a
        failed reopen — the switch stays disarmed until :meth:`close`)."""
        return self._snapshot.fd is not None

    def points_at_current_file(self) -> bool:
        """Whether this arm's fd points at the file CURRENTLY at its own
        destination path — a thin public passthrough to
        :meth:`~reyn.runtime.diagnostic_snapshot.DiagnosticSnapshot.
        points_at_current_file`, exposed so a test can witness
        :meth:`rearm`'s own reopen-on-external-change behaviour through
        the public surface rather than reaching into this arm's private
        snapshot."""
        return self._snapshot.points_at_current_file()

    def rearm(self) -> bool:
        """Re-point the one process-wide timer :data:`_seconds` into the
        future against this arm's own fd (reopening first if something
        external — a cleanup tool, an operator ``rm`` — deleted or moved
        the file out from under it since it was last opened; see
        :meth:`~reyn.runtime.diagnostic_snapshot.DiagnosticSnapshot.
        points_at_current_file`'s own docstring for why this matters: a
        write against an orphaned fd still succeeds, silently, into a
        file nobody can find). Returns whether a timer is now pending."""
        from reyn.runtime.stall_trace import arm as _arm

        if self._snapshot.fd is None:
            return False
        if not self._snapshot.points_at_current_file():
            self._snapshot.reset()
            if self._snapshot.fd is None:
                self._logger.error(
                    "%s: could not reopen the tripwire's own stall-dump snapshot after "
                    "it was removed or moved out from under it", self._label,
                )
                return False
        _arm(self._seconds, file=self._snapshot.fd, repeat=False)
        return True

    def mark_fired(self) -> None:
        """Call ONCE, right after the caller has OBSERVED (via the
        ``stack_dumped`` proxy) that a dump this arm scheduled actually
        fired — truncates and reopens the snapshot's fd so the NEXT
        episode's dump overwrites cleanly, ready before it is next armed.
        Never call this on an ordinary re-arm — see the class docstring's
        truncate-timing trap."""
        self._snapshot.reset()

    def close(self) -> None:
        """Disarm BEFORE closing the fd (#5877: the reverse order would let a
        still-pending timer fire against an already-closed, possibly
        already-reused fd number). Idempotent."""
        from reyn.runtime.stall_trace import disarm as _disarm

        if self._snapshot.fd is None:
            return
        _disarm()
        self._snapshot.close()


async def watch_event_loop(
    tripwire: LoopTripwire,
    *,
    on_stall: "Callable[[float], None]",
    on_recovered: "Callable[[], None] | None" = None,
    stack_dump: "StallDumpArm | None" = None,
    turn_active: "Callable[[], bool | None] | None" = None,
    on_tick: "Callable[[float, float], None] | None" = None,
    tick_seconds: float = _TICK_SECONDS,
    clock: "Callable[[], float]" = time.perf_counter,
    sleep: "Callable[[float], Any]" = asyncio.sleep,
) -> None:
    """The ONE tick loop (#5898): sleep ``tick_seconds``, measure how late
    the wake-up landed, feed :class:`LoopTripwire`, re-arm the dead-man's
    switch, and hand the once-per-episode notices to the caller.

    ``on_stall(lateness_ms)`` fires the FIRST tick of each stall episode
    (``LoopTripwire.observe``'s own once-only return); ``on_recovered()``
    fires once when the episode ends. ``turn_active`` (if the caller has
    such a reading) and ``on_tick(now, lateness_ms)`` (the CUI's own
    pump/keys window bookkeeping) are optional context hooks. ``clock``
    and ``sleep`` are injected so the loop's own timing rule is testable
    with the clock as an INPUT — a test supplies a clock that jumps, never
    a ``time.sleep`` on the loop it is measuring (CLAUDE.md: a duration is
    an input you supply, not a wait).

    **#5977 ① ordering (lead-coder BLOCKING, PR #5980)**: whether THIS
    tick's re-arm should happen is decided BEFORE the tick's own lateness
    is known to mean "the PREVIOUS arm just fired" — checking the two in
    the reverse order (re-arm, THEN read ``stack_dumped``, THEN record)
    always re-arms once more than intended, because the very tick that
    detects a fire has, by that point, already scheduled the NEXT pending
    timer — a real ``faulthandler`` commitment nothing then cancels. So
    ``armed_last_tick`` carries "is there a pending arm to read back"
    ACROSS ticks: each tick first asks whether the arm made LAST tick
    fired (if any), records it, and only THEN decides whether to make a
    NEW one — never both in the same breath.

    Runs until cancelled (the caller's shutdown); the ``finally`` releases
    the process-wide timer and this watcher's fd.
    """
    last = clock()
    armed_last_tick = False
    if stack_dump is not None and tripwire.should_arm_stack_dump():
        # Arm for the FIRST wait too — a stall on the very first tick would
        # otherwise go undumped (#5870 stage 1).
        armed_last_tick = stack_dump.rearm()
    try:
        while True:
            await sleep(tick_seconds)
            now = clock()
            lateness_ms = (now - last - tick_seconds) * 1000
            last = now
            stack_dumped: "bool | None" = None
            if armed_last_tick:
                # Best-effort proxy for "did the pending dump just fire" —
                # the SAME comparison observe() makes internally, never a
                # readback of faulthandler's (nonexistent) fired state.
                stack_dumped = lateness_ms > tripwire.threshold_ms
                if stack_dumped:
                    tripwire.record_stack_dump()
                    if stack_dump is not None:
                        # #5977 ②: truncate + reopen for the NEXT episode
                        # NOW, right after observing this fire — never on
                        # an ordinary re-arm (StallDumpArm.mark_fired's own
                        # docstring: the truncate-timing trap).
                        stack_dump.mark_fired()
            if on_tick is not None:
                on_tick(now, lateness_ms)
            active = turn_active() if turn_active is not None else None
            fired = tripwire.observe(lateness_ms, turn_active=active, stack_dumped=stack_dumped)
            # #5977 ① (lead-coder BLOCKING follow-up, PR #5980): the re-arm
            # decision must run AFTER `observe()`, not before — `observe()`
            # is what resets the per-episode dump allowance at a recovery
            # transition (mirrors `_fired`'s own reset). Deciding earlier
            # reads a STALE "already dumped" flag on the exact tick a
            # recovery happens, leaving nothing armed to catch the very
            # next episode's onset (measured: a scripted second episode
            # went undumped entirely under the earlier ordering). Skipping
            # the re-arm IS the suppression — no separate faulthandler
            # state to cancel, since each arm is one-shot and replaces the
            # previous pending timer; a fire detected THIS tick has
            # already closed via `record_stack_dump()` above, so it never
            # re-arms a further timer for the SAME still-ongoing episode.
            armed_last_tick = bool(
                stack_dump is not None and tripwire.should_arm_stack_dump() and stack_dump.rearm()
            )
            if fired is not None:
                on_stall(fired)
            elif tripwire.consume_recovered() and on_recovered is not None:
                on_recovered()
    finally:
        if stack_dump is not None:
            stack_dump.close()
