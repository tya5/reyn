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
from typing import Any, Callable

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

#: #5977 ①: a per-episode cap of 1 (see :meth:`LoopTripwire.should_arm_stack_
#: dump`) stops the SELF-AMPLIFICATION within one stall — each dump's own
#: synchronous write cost adding to the very lateness that risked triggering
#: the next one — but says nothing about a run of many SEPARATE, genuinely
#: distinct short stalls, each legitimately producing its own single dump.
#: This is the ceiling on that SUM, the backstop the band's "who stops this
#: if it repeats" question needs an answer to (owner-hit, #5977). Chosen, not
#: measured — no comparable session-length dump-count data exists yet
#: (architect flagged the number as their own to propose; this is a
#: reasoned default, a single constant to change if a different one lands):
#: at ~14.6 KB per dump (#5977 architect finding, real machine), 10 dumps is
#: ~150 KB of evidence — ample to diagnose a session's worth of stalls —
#: while keeping the cumulative self-inflicted dump cost this issue exists
#: to bound from growing without limit.
_SESSION_DUMP_CAP = 10


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

    def __init__(
        self, *, threshold_ms: float = _TRIPWIRE_MS, session_dump_cap: int = _SESSION_DUMP_CAP,
    ) -> None:
        self._threshold_ms = threshold_ms
        self._max_lateness_ms = 0.0
        self._fired = False
        #: #5977 ①: has the CURRENT (still-ongoing) episode already produced
        #: a stack dump — reset alongside ``_fired`` at recovery, below,
        #: since both answer "has THIS episode already told its one story."
        self._dumped_this_episode = False
        self._session_dump_cap = session_dump_cap
        self._session_dump_count = 0
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

    @property
    def session_dump_cap(self) -> int:
        """The session-total dump ceiling this instance was constructed
        with (#5977 ①) — exposed alongside :attr:`session_dump_count` so a
        caller's cap-reached notice needs no second, easily-drifting copy
        of the number."""
        return self._session_dump_cap

    @property
    def session_dump_count(self) -> int:
        """How many stack dumps this session has produced so far (#5977 ①)
        — exposed so a caller's own cap-reached notice, and a test, can read
        the count directly rather than re-deriving it from ticks."""
        return self._session_dump_count

    def should_arm_stack_dump(self) -> bool:
        """Whether the caller's dead-man's switch (:class:`StallDumpArm`)
        should be RE-ARMED this tick (#5977 ①).

        ``False`` in two cases: the CURRENT episode already produced a dump
        (:meth:`record_stack_dump` set that — recovery below clears it for
        the NEXT episode), or the session-total cap is already reached.
        Consulted BEFORE ``StallDumpArm.rearm()`` — there is no separate
        ``faulthandler`` state to cancel; skipping the re-arm IS the
        suppression, since each arm is one-shot and replaces the previous
        pending timer (see :class:`StallDumpArm`'s own docstring)."""
        if self._dumped_this_episode:
            return False
        return self._session_dump_count < self._session_dump_cap

    def record_stack_dump(self) -> bool:
        """Report that an armed dump actually fired this tick (the caller's
        own ``stack_dumped`` proxy turned ``True``) — closes this episode's
        one-shot allowance and advances the session total.

        Returns whether the session cap was JUST reached — ``==``, not
        ``>=``: :meth:`should_arm_stack_dump` refuses to let this method be
        called at all once the cap is already met, so the count can only
        ever CROSS the cap on the one tick this returns ``True`` — no
        separate once-only flag needed the way :meth:`observe`'s own
        ``_fired`` is (there, a caller CAN keep calling ``observe`` past
        the first stall tick; here, the caller structurally cannot keep
        calling this past the cap). The caller logs the always-visible ③
        notice exactly on that tick, never silently and never repeated —
        "抑制しても『抑制した』ことが分からなければ、次の人は『stall が
        無かった』と読みます" (#5977, lead-coder)."""
        self._dumped_this_episode = True
        self._session_dump_count += 1
        return self._session_dump_count == self._session_dump_cap

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


def stall_dump_cap_reached_log_line(session_dump_count: int, session_dump_cap: int) -> str:
    """The always-visible (#5977 ③) notice that the session-total stack-dump
    cap was just reached — logged exactly once, at ``logger.warning``, the
    SAME unconditional surface :func:`stall_recovered_log_line` already
    uses, never gated behind ``REYN_PROF_DUMP``. Without this line, a
    session that hit the cap and a session that never stalled again look
    identical to the next reader: the stall/recovery notices keep firing
    (:meth:`LoopTripwire.observe` is untouched by the cap), so silence on
    the DUMP side alone would read as "no more stalls," not "stopped
    recording them" — the same silence-hides-two-states shape #4761
    exists to close, one level up."""
    return (
        f"stall dump cap reached ({session_dump_count}/{session_dump_cap} this "
        "session) — stalls will still be reported above, but no further "
        "stack dumps will be written"
    )


class StallDumpArm:
    """The per-tick ``faulthandler`` dead-man's switch and the fd it dumps
    into — the ONE implementation of the #5877/#5873 rules (#5898: lifted
    out of ``TextualChatApp._watch_loop_responsiveness`` so ``reyn:web``'s
    tripwire dumps the same way, not a second copy of the same hazards).

    **Why an fd of its own (#5877, architect ruling, real-machine
    measurement)**: ``faulthandler.dump_traceback_later`` captures the
    ``file`` argument's underlying FILE-DESCRIPTOR NUMBER at arm time, not
    a live object. A caller that stays armed across MANY of its own calls
    must therefore arm against an fd nothing else can close and reuse: a
    pending timer armed against a stream object whose fd number got reused
    for something ELSE (an ``execnet`` socket, in the CI hang #5877
    explains) silently dumps THERE instead — hanging the reader on the
    other end. So this opens its OWN fd, once, against the root logger's
    ``FileHandler`` path (never borrowing the handler's own stream) and
    holds it for its whole lifetime. **No ``FileHandler`` installed means
    :meth:`open` returns ``None`` and nothing ever arms** — deliberately,
    not a fail-open: a dump with no genuinely stable destination was never
    a safe thing to attempt.

    **Why the inode check (#5873)**: log rotation (``RotatingFileHandler``)
    renames the path this fd was opened against out from under it on every
    rollover — the underlying FILE moves to ``.1``, ``.2``, … until it is
    unlinked past ``backup_count``. Left unhandled, every dump after the
    first rollover would land in an ever-more-stale, eventually DELETED
    generation nobody reads. Each :meth:`rearm` therefore compares
    ``os.stat(path).st_ino`` against ``os.fstat(fd).st_ino`` —
    deterministic, cut on the file identity changing, not a clock — and on
    a mismatch disarms, closes the stale fd and opens a fresh one against
    the same path before re-arming.

    **Why ``repeat=False`` and re-arm every tick**: each re-arm cancels and
    replaces the PENDING one-shot timer, so it only ever actually FIRES
    when a tick fails to arrive in time to re-arm it — i.e. while the very
    thing it watches is blocked (see ``stall_trace.arm``'s own docstring).
    """

    def __init__(self, *, seconds: float, log_path: str, fd: int, logger: logging.Logger, label: str) -> None:
        self._seconds = seconds
        self._log_path = log_path
        self._fd: "int | None" = fd
        self._logger = logger
        self._label = label

    @classmethod
    def open(
        cls, *, seconds: float, log_path: "str | None", logger: logging.Logger, label: str,
    ) -> "StallDumpArm | None":
        """Open the dump fd once; ``None`` when there is no log path (no
        ``FileHandler`` installed) or it cannot be opened — in both cases
        the dead-man's switch simply never arms for this watcher."""
        if log_path is None:
            return None
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        except OSError:
            logger.exception("%s: could not open the tripwire's own stall-dump fd", label)
            return None
        return cls(seconds=seconds, log_path=log_path, fd=fd, logger=logger, label=label)

    @property
    def armed(self) -> bool:
        """Whether this arm currently holds a usable fd (False after a
        failed reopen — the switch stays disarmed until :meth:`close`)."""
        return self._fd is not None

    def points_at(self, path: str) -> bool:
        """Whether this arm's fd is the CURRENT file at *path* (inode
        identity — the #5873 question :meth:`rearm` answers before every
        re-arm, exposed so a test can witness a reopen after a rotation
        without reading the fd itself)."""
        if self._fd is None:
            return False
        try:
            return os.stat(path).st_ino == os.fstat(self._fd).st_ino
        except OSError:
            return False

    def rearm(self) -> bool:
        """Re-point the one process-wide timer :data:`_seconds` into the
        future against this arm's own fd (reopening it first if a rotation
        moved the file). Returns whether a timer is now pending."""
        from reyn.runtime.stall_trace import arm as _arm
        from reyn.runtime.stall_trace import disarm as _disarm

        if self._fd is None:
            return False
        try:
            stale = os.stat(self._log_path).st_ino != os.fstat(self._fd).st_ino
        except OSError:
            stale = False
        if stale:
            _disarm()
            try:
                os.close(self._fd)
            except OSError:
                pass
            try:
                self._fd = os.open(self._log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            except OSError:
                self._logger.exception(
                    "%s: could not reopen the tripwire's own stall-dump fd after a log rotation",
                    self._label,
                )
                self._fd = None
                return False
        _arm(self._seconds, file=self._fd, repeat=False)
        return True

    def close(self) -> None:
        """Disarm BEFORE closing the fd (#5877: the reverse order would let a
        still-pending timer fire against an already-closed, possibly
        already-reused fd number). Idempotent."""
        from reyn.runtime.stall_trace import disarm as _disarm

        if self._fd is None:
            return
        _disarm()
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


async def watch_event_loop(
    tripwire: LoopTripwire,
    *,
    on_stall: "Callable[[float], None]",
    on_recovered: "Callable[[], None] | None" = None,
    stack_dump: "StallDumpArm | None" = None,
    turn_active: "Callable[[], bool | None] | None" = None,
    on_tick: "Callable[[float, float], None] | None" = None,
    on_dump_cap_reached: "Callable[[int, int], None] | None" = None,
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

    ``on_dump_cap_reached(session_dump_count, session_dump_cap)`` (#5977
    ①③): fires once, the tick the session-total dump cap is reached — see
    :meth:`LoopTripwire.record_stack_dump`'s own once-only return. Passed
    through rather than logged here directly so each caller (this module
    has no logger of its own) reports it on its own already-established
    surface, matching ``on_stall``/``on_recovered``'s own shape.

    Runs until cancelled (the caller's shutdown); the ``finally`` releases
    the process-wide timer and this watcher's fd.
    """
    last = clock()
    if stack_dump is not None and tripwire.should_arm_stack_dump():
        # Arm for the FIRST wait too — a stall on the very first tick would
        # otherwise go undumped (#5870 stage 1).
        stack_dump.rearm()
    try:
        while True:
            await sleep(tick_seconds)
            now = clock()
            lateness_ms = (now - last - tick_seconds) * 1000
            last = now
            stack_dumped: "bool | None" = None
            # #5977 ①: only re-arm while this episode hasn't dumped yet and
            # the session cap isn't reached — skipping the re-arm IS the
            # suppression (no separate faulthandler state to cancel; each
            # arm is one-shot and replaces the previous pending timer).
            if (
                stack_dump is not None
                and tripwire.should_arm_stack_dump()
                and stack_dump.rearm()
            ):
                # Best-effort proxy for "did the pending dump just fire" —
                # the SAME comparison observe() makes internally, never a
                # readback of faulthandler's (nonexistent) fired state.
                stack_dumped = lateness_ms > tripwire.threshold_ms
                if stack_dumped and tripwire.record_stack_dump() and on_dump_cap_reached is not None:
                    on_dump_cap_reached(tripwire.session_dump_count, tripwire.session_dump_cap)
            if on_tick is not None:
                on_tick(now, lateness_ms)
            active = turn_active() if turn_active is not None else None
            fired = tripwire.observe(lateness_ms, turn_active=active, stack_dumped=stack_dumped)
            if fired is not None:
                on_stall(fired)
            elif tripwire.consume_recovered() and on_recovered is not None:
                on_recovered()
    finally:
        if stack_dump is not None:
            stack_dump.close()
