"""Event-loop responsiveness instrumentation for the inline CUI (#3539).

Two layers, because they answer different questions and only one of them can be
switched on in advance.

**A tripwire that is always on.** The symptom this exists for — "the UI froze
while a reply streamed" — arrives unannounced, so an opt-in probe is only ever
enabled *after* someone has already lost the occurrence they wanted to measure.
#3638 closed exactly that way: by the time anyone could look, the symptom had
stopped happening. The tripwire therefore runs unconditionally and costs a
comparison against a float per tick; when the loop is late by more than
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

Measured baseline this was built against (real TUI, real model, 463 chunks):
work 0.31 ms/chunk, wait 17.84 ms/chunk, **0** loop stalls over 12 ms. The
tripwire is silent on a healthy loop by construction, not by tuning.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

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
        #: (:meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
        #: _watch_loop_responsiveness`) put a recovery notice on a surface
        #: that needs no prior setup — the same "visible with the shipped
        #: config" bar the once-only stall notice already clears.
        self._just_recovered = False

    @property
    def max_lateness_ms(self) -> float:
        """The worst lateness observed so far, in milliseconds."""
        return self._max_lateness_ms

    @property
    def fired(self) -> bool:
        """Whether the threshold has been crossed at least once."""
        return self._fired

    def consume_recovered(self) -> bool:
        """Whether the loop just recovered from a stall — ``True`` at most
        once per episode, consumed on read (mirrors :meth:`observe`'s own
        once-only return for the stall side) so a caller logging this on a
        default-visible surface doesn't repeat it either."""
        recovered = self._just_recovered
        self._just_recovered = False
        return recovered

    def observe(
        self, lateness_ms: float, *, pump_ticks: "int | None" = None,
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
        """
        if lateness_ms > self._max_lateness_ms:
            self._max_lateness_ms = lateness_ms
        if lateness_ms <= self._threshold_ms:
            if self._in_stall:
                self._in_stall = False
                self._just_recovered = True
                write_record(
                    "tripwire_recovered",
                    lateness_ms=round(lateness_ms, 1),
                    **({} if pump_ticks is None else {"pump_ticks": pump_ticks}),
                )
            return None
        self._in_stall = True
        now = time.monotonic()
        if (
            self._last_recorded_monotonic is None
            or now - self._last_recorded_monotonic >= _RECORD_INTERVAL_S
        ):
            write_record(
                "tripwire",
                lateness_ms=round(lateness_ms, 1),
                **({} if pump_ticks is None else {"pump_ticks": pump_ticks}),
            )
            self._last_recorded_monotonic = now
        if self._fired:
            return None
        self._fired = True
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
    return (
        f"the interface was unresponsive for {lateness_ms / 1000:.1f}s"
        f"{ticks_note}{keys_note} — re-run with {_DUMP_ENV}=<path> to record "
        "what it was doing"
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
    stall (H1 ruled out for this episode); if they're equal, the pump
    never advanced at all (H1 the leading candidate).
    """
    if pump_ticks is None:
        return "the interface recovered from the stall reported above"
    return (
        "the interface recovered from the stall reported above "
        f"(pump heartbeat at {pump_ticks} — compare against the stall "
        "notice's own reading)"
    )
