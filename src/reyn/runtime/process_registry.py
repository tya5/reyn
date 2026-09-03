"""reyn.runtime.process_registry — a PID-keyed marker per reyn CLI
process, so reyn can answer "how many of me are alive right now, and
who started each one" without shelling out to ``ps`` (#5226).

WHY THIS EXISTS
    Owner's own observation (2026-08-21, relayed by lead-coder): "I only
    launched one reyn session, so the rest are your own cleanup misses."
    lead-coder's own real-machine trace confirmed it — 12 ``reyn``/
    ``reyn:chat`` processes, 11 of them abandoned, the oldest 11 days —
    and had no way to answer "how many, and whose" except a manual
    ``ps -eo pid,etime,comm`` + ``lsof -a -p <pid> -d cwd`` (reyn
    rewrites its own process NAME via ``reyn.runtime.proctitle``, so
    ``comm`` alone cannot say which workspace a listed PID belongs to).
    A reyn session is designed to persist until something explicitly
    ends it — no bounding subject exists today (charter Q1: "who stops
    this if it repeats" — nobody). This module does not add a bounding
    subject (kill/TTL cleanup is explicitly OUT OF SCOPE, an owner-level
    decision once the count is actually visible) — it makes the count
    and the "whose" both readable, which is the charter's own weaker,
    prerequisite claim (Q2: visible with the shipped config).

WHAT GETS RECORDED, AND WHY NOT MORE
    ``{pid, ppid, cwd, subcommand, started_at}`` — exactly the fields a
    human already has to reconstruct by hand today (lead-coder's own
    ``lsof -d cwd`` trace), never fabricated attribution reyn cannot
    actually know. Deliberately NOT full ``argv`` and NOT any path
    beyond ``cwd`` — mirrors ``reyn.runtime.proctitle``'s own explicit
    stance against leaking more than the minimum into anything an
    operator (or, here, a doctor report) can read back: prompts, file
    arguments and workspace-internal paths belong to the invocation,
    not to a liveness marker.

    Plus (#5350) ``{agent_name, broker_session_id}`` — both absent
    (``None``) until a later call to :func:`record_process_identity`
    sets one or both; never guessed, never derived from ``cwd`` (see
    the "IDENTITY vs LIVENESS" section below for why).

WHERE THE MARKER LIVES
    ``~/.reyn/processes/<pid>.json`` — a PID-keyed filename, never a
    shared subtree multiple processes both write into. This matters:
    ``~/.reyn/plugins/<name>/`` is a KNOWN concurrency hazard (#3212) —
    two sessions installing the same plugin concurrently can clobber
    each other's whole directory tree, because the path both write to
    is the SAME for both. A PID is unique to one process for the
    process's own lifetime, so this module's own writes never collide
    with another process's — only with a REUSED pid after this process
    already exited without cleaning up, which :func:`live_processes`'s
    own liveness re-check (below) already treats as "stale, reap it"
    before ever trusting stale content.

LIVENESS: REUSE, DON'T REINVENT (#5296's own lesson)
    :func:`reyn.data.index.build_lock.pid_alive` already exists,
    canonicalized for exactly this "is a marker's PID still real"
    question — reused here verbatim, not reimplemented. NOT
    :func:`reyn.api.safe.process.pid_alive` (semantically identical
    ``os.kill(pid, 0)`` probe, but deliberately scoped to the sandboxed
    safe-mode python surface per that module's own docstring — pulling
    a sandbox-scoped helper into trusted core runtime code blurs a
    boundary that module exists to keep, even though today's
    implementations happen to match). The duplication between the two
    is itself a separate, disclosed finding — NOT unified here (out of
    this issue's own scope; a consolidation would need to weigh
    ``api/safe/``'s own sandbox-boundary reasoning, which this module
    has no business deciding).

IDENTITY vs LIVENESS (#5350, owner-observed incident 2026-08-30)
    A real incident: an operator-side script joined "which OS process is
    this reyn/broker identity" on ``cwd`` — and sent ``SIGTERM`` to
    unrelated ``zsh``/``nvim`` processes that merely happened to share a
    directory with a registered agent. ``cwd`` never carries identity
    (this module's own markers already recorded it as diagnostic
    metadata only, never a join key — see :func:`live_processes`'s own
    ``cwd``-based reap-event lookup, which resolves a PROJECT root from
    it, never an IDENTITY). Architect ruling (#5350): identity must be
    answered from a RECORDED fact (a marker this process wrote about
    itself), never derived from ``cwd``/``comm`` (rewritten by
    :mod:`reyn.runtime.proctitle`) or from ``list_sessions().active``
    (registration, not liveness). :func:`record_process_identity` +
    :func:`process_for_agent` close this — a process states its own
    ``agent_name``/``broker_session_id`` (it already knows both; no
    guessing), and a reader filters :func:`live_processes` by that
    RECORDED field, never by position (``cwd``). Liveness stays a
    SEPARATE question this module does not answer for a reyn agent
    (that is ``.reyn/agents/<name>/state/``'s own mtime, per #5350's own
    table) — this module only ever answers "is the OS process alive"
    (:func:`~reyn.data.index.build_lock.pid_alive`), a narrower claim.

    Explicitly OUT OF SCOPE (same posture as the rest of this module,
    #5350 architect ruling): no kill/TTL decision lives here — this
    closes "identity is readable", never "an identity gets acted on".

WHAT THIS MODULE DOES NOT DO
    No cleanup of another process's live marker, ever (D-2 posture,
    matching ``reyn doctor``'s own report-only rule this module's
    reader feeds) — a marker is only ever removed by (a) its OWNING
    process's own :func:`atexit` handler on a graceful exit, or (b) a
    READER reaping a marker whose PID is confirmed DEAD via
    :func:`~reyn.data.index.build_lock.pid_alive` (removing metadata
    about a process that is provably already gone is not "killing"
    anything). No process enumeration of its own, either — "walk every
    PID on the machine" is the OS's job (``ps``'s own domain); this
    module only ever reads the markers processes wrote about
    THEMSELVES.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Final

from reyn.data.index.build_lock import pid_alive

if TYPE_CHECKING:
    from reyn.runtime.tracked_tasks import TrackedTaskSet

logger = logging.getLogger(__name__)

PROCESSES_DIR: Final[Path] = Path.home() / ".reyn" / "processes"


def _marker_path(pid: int) -> Path:
    return PROCESSES_DIR / f"{pid}.json"


def _tmp_marker_path(pid: int) -> Path:
    """#5346: the staging path :func:`register_process` writes to BEFORE
    the atomic rename onto :func:`_marker_path`'s own name — never a real
    marker itself. ``*.json`` globs (both here and in
    :func:`live_processes`) never match this suffix, so a reader can never
    mistake a still-being-written marker for a real one; only
    :func:`live_processes`'s own reap pass (below) ever looks at this
    suffix at all, and only to remove one whose owning PID is confirmed
    dead."""
    return PROCESSES_DIR / f"{pid}.json.tmp"


def register_process(subcommand: "str | None") -> None:
    """Write this process's own marker and register its own cleanup.

    Call exactly once, at CLI startup — the SAME hook point
    :func:`reyn.runtime.proctitle.set_process_title` already uses
    (``interfaces/cli/__init__.py:main()``), so the two stay paired:
    whatever ``ps`` can show as this process's NAME, this marker can
    show as its own record.

    Best-effort throughout: a marker write failure (permissions, a
    missing home directory, a full disk) must never block reyn from
    starting — this is a diagnostic aid, not a precondition (mirrors
    ``proctitle.py``'s own "a diagnostic aid that can stop a program
    from starting is worse than the diagnosis it offers").

    #5346: writes to a ``.tmp`` staging path first, then
    :meth:`~pathlib.Path.replace` (``os.replace``, atomic on the same
    filesystem on POSIX) onto the real marker name — a reader
    (:func:`live_processes`) can therefore only ever see this pid's
    marker as either "fully written" or "not there yet", never
    partial. Found via a real CI failure (#5345/#5226, lead-coder's own
    observation): a reader that raced a plain ``write_text`` straight to
    the final path could read a truncated file mid-write."""
    pid = os.getpid()
    marker = {
        "pid": pid,
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "subcommand": subcommand,
        "started_at": time.time(),
        # #5714 (architect ruling): a process can host N Sessions (#5694
        # confirmed 1 process : N Session — AgentRegistry.ensure_running
        # runs every agent's own session.run() as an asyncio task in the
        # SAME process, never a separate OS process per agent). A
        # SINGULAR agent_name/broker_session_id field here was the wrong
        # SHAPE for that fact — the second Session constructed in one
        # process silently overwrote the first's identity, and
        # process_for_agent(first_agent) started returning [] while that
        # Session was still alive (#5714's own reproduced incident).
        # Empty at registration (register_process runs at CLI startup,
        # before any Session is constructed) — grows one entry per
        # (agent_name, sid) via :func:`record_process_identity`, keyed so
        # a Session rebuilt under the SAME key overwrites its own entry
        # rather than accumulating a duplicate (bounded by "how many
        # DISTINCT (agent_name, sid) keys this process has ever hosted" —
        # charter Q1's own answer, per the ruling).
        "sessions": [],
        # #5709: absent (never guessed) until this process's own turn
        # loop actually starts running and arms ProcessLoopBeatDriver —
        # see that class's own docstring. A marker that never gains this
        # field is honest: it means a process that registered but whose
        # loop never started (or never reached the arming seam).
        "last_loop_beat_at": None,
    }
    try:
        PROCESSES_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _tmp_marker_path(pid)
        tmp_path.write_text(json.dumps(marker), encoding="utf-8")
        tmp_path.replace(_marker_path(pid))
    except OSError:
        logger.warning(
            "process_registry: failed to write launch marker for pid %d "
            "(diagnostic-only, does not block startup)", pid, exc_info=True,
        )
        return
    atexit.register(_cleanup, pid)


def _cleanup(pid: int) -> None:
    """Remove THIS process's own marker on a graceful exit. Never called
    for another process's marker — ``atexit`` only ever runs in the
    process that registered it. A process that dies WITHOUT reaching a
    graceful exit (SIGKILL, a hard crash) leaves its marker behind by
    construction — :func:`live_processes`'s own liveness re-check is
    what reaps that case, not this function."""
    try:
        _marker_path(pid).unlink()
    except OSError:
        pass


def unregister_process(pid: "int | None" = None) -> None:
    """The public counterpart to :func:`register_process` — removes this
    process's own marker right now AND cancels the ``atexit`` handler
    :func:`register_process` armed, so nothing fires again later.

    Exists because #5326's TESTS-READ(B) review found two real problems
    with calling the private :func:`_cleanup` directly (as this module's
    own tests originally did): (a) CLAUDE.md's own rule — "a test must
    not depend on private state... if neither exists, that absence is
    the finding" — and no PUBLIC way to undo a registration existed; (b)
    calling ``_cleanup`` alone still leaves the ``atexit`` handler armed,
    so it fires again at interpreter shutdown — against whatever
    ``PROCESSES_DIR`` is live AT THAT POINT, not the one active when
    ``register_process`` was called. A test that monkeypatches
    ``PROCESSES_DIR`` to an isolated ``tmp_path`` and calls only
    ``_cleanup`` in its own teardown leaves that stale handler armed
    against the REAL ``~/.reyn/processes/`` for the rest of the
    interpreter's life — harmless only by the accident that the pid
    being unlinked is always this test process's own (which no real reyn
    process can also be using while alive), a guarantee nothing in the
    code enforced or documented until now.

    Idempotent: ``atexit.unregister`` on a handler that was already
    cancelled (or never armed) is a documented no-op, and ``_cleanup``
    already swallows a missing marker."""
    if pid is None:
        pid = os.getpid()
    _cleanup(pid)
    atexit.unregister(_cleanup)


# #5714: the sid default when a caller doesn't have one to hand —
# duplicates registry.py's own ``_DEFAULT_SID = "main"`` literal rather
# than importing it: process_registry.py sits BELOW registry.py in this
# repo's own layering (registry.py — the higher-level AgentRegistry — is
# what calls into this module for process-marker facts, never the other
# way around), so importing registry.py here risks the exact import-
# cycle this module has stayed free of throughout. "main" is the one
# well-known sid literal every caller (registry.py, Session's own
# ``session_id`` param default) already agrees on.
_DEFAULT_SID = "main"


def record_process_identity(
    *,
    agent_name: "str | None" = None,
    sid: str = _DEFAULT_SID,
    broker_session_id: "str | None" = None,
    pid: "int | None" = None,
) -> None:
    """#5350, reshaped by #5714 (architect ruling) — a process records
    ONE Session's own identity onto its already-written marker, once it
    actually knows it (``register_process`` runs at CLI startup, before
    that is usually resolved). Never a guess: the caller is always the
    process itself (or code acting on its behalf) stating a fact it
    already has — never a lookup, never derived from ``cwd``.

    #5714: the marker's own identity field changed SHAPE from a single
    ``agent_name``/``broker_session_id`` pair to a collection of
    ``sessions`` entries keyed by ``(agent_name, sid)`` — #5694 confirmed
    a process can host N Sessions as N concurrent asyncio tasks, so a
    singular field was the wrong grain for the fact it recorded (the
    SECOND Session constructed in a process silently overwrote the
    FIRST's identity; ``process_for_agent`` on the first agent then
    returned ``[]`` while that Session was still alive — #5714's own
    reproduced incident). This function is now an UPSERT into that
    collection: an existing entry for the SAME ``(agent_name, sid)`` is
    updated in place (the ruling's own "同じ key の session が作り直さ
    れたら上書き" — a Session genuinely rebuilt under the same key
    reuses its own entry, never accumulates a duplicate); no matching
    entry appends a new one.

    Only fields the caller actually passes are applied to that entry
    (``None`` here means "nothing new to say about broker_session_id",
    not "clear it" — mirrors the pre-#5714 single-field semantics, now
    scoped to one entry instead of the whole marker). ``agent_name`` and
    ``broker_session_id`` both absent (``None``) is a pure no-op — there
    is no entry to key by. Best-effort, matching this module's own
    posture throughout: no marker (the process never called
    :func:`register_process`, or its write failed) degrades to a silent
    no-op, never an exception that could interrupt the caller's own real
    work over a diagnostic aid.
    """
    if agent_name is None and broker_session_id is None:
        return
    if pid is None:
        pid = os.getpid()
    marker_path = _marker_path(pid)
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    sessions = data.setdefault("sessions", [])
    entry = next(
        (
            e for e in sessions
            if e.get("agent_name") == agent_name and e.get("sid") == sid
        ),
        None,
    )
    if entry is None:
        entry = {
            "agent_name": agent_name, "sid": sid,
            "broker_session_id": None, "ended_at": None,
        }
        sessions.append(entry)
    if agent_name is not None:
        entry["agent_name"] = agent_name
    if broker_session_id is not None:
        entry["broker_session_id"] = broker_session_id
    try:
        tmp_path = _tmp_marker_path(pid)
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(marker_path)
    except OSError:
        logger.warning(
            "process_registry: failed to record identity for pid %d "
            "(diagnostic-only, does not block the caller)", pid, exc_info=True,
        )


def record_session_ended(
    *, agent_name: str, sid: str = _DEFAULT_SID, pid: "int | None" = None,
) -> None:
    """#5714 (architect ruling, point ③): the SAME callback #5694 already
    wired (``AgentRegistry._on_session_run_task_done``, the one
    done-callback for every ``(name, sid)`` background ``session.run()``
    task) also presses THIS — not a second lifecycle mechanism. Sets
    ``ended_at`` on the ONE matching ``sessions`` entry for
    ``(agent_name, sid)`` — every OTHER entry this process hosts is
    untouched. Necessary once the marker's identity became a collection
    (#5714): without this, a Session whose task genuinely ended would
    keep showing as "hosted" forever, a new lie in the SHAPE this issue
    exists to fix (a collection with no way to mark an entry stale is
    exactly as dishonest as the single overwritten field it replaces).

    A no-op — never an error — when no matching entry exists (the
    Session's own identity was never recorded via
    :func:`record_process_identity`, e.g. it died before ``Session.
    __init__`` reached that call): there is nothing to mark ended, and
    fabricating an entry here would invent an identity this module never
    actually observed. Best-effort, matching every other write in this
    module: an OSError on the write-back is logged and swallowed, never
    propagated into the caller's own real teardown work."""
    if pid is None:
        pid = os.getpid()
    marker_path = _marker_path(pid)
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    sessions = data.get("sessions") or []
    entry = next(
        (
            e for e in sessions
            if e.get("agent_name") == agent_name and e.get("sid") == sid
        ),
        None,
    )
    if entry is None:
        return
    entry["ended_at"] = time.time()
    try:
        tmp_path = _tmp_marker_path(pid)
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(marker_path)
    except OSError:
        logger.warning(
            "process_registry: failed to record session end for pid %d "
            "(agent_name=%r, sid=%r) (diagnostic-only, does not block "
            "the caller)", pid, agent_name, sid, exc_info=True,
        )


def process_for_agent(agent_name: str) -> "list[dict]":
    """#5350, reshaped by #5714 — every currently-alive process that has
    RECORDED (via :func:`record_process_identity`) at least one
    ``sessions`` entry whose own ``agent_name`` matches *agent_name*
    (via :func:`live_processes`, which already reaps dead-PID markers as
    it reads). NEVER matched by ``cwd`` — the architect-named incident
    this exists to prevent: an unrelated process (a shell, an editor)
    sharing a directory with a reyn agent is not that agent, and must
    never be returned here. #5714: a process hosting SEVERAL agents now
    correctly matches a query for ANY of them (the pre-#5714 defect this
    issue closes — a singular field could only ever answer for whichever
    agent was constructed MOST RECENTLY in that process). Ordinarily a
    list of 0 or 1 — more than one means two SEPARATE processes both
    recorded a session under the SAME ``agent_name`` (a caller error
    upstream, not something this function corrects or hides)."""
    return [
        m for m in live_processes()
        if any(e.get("agent_name") == agent_name for e in m.get("sessions", []))
    ]


def process_for_broker_session(broker_session_id: str) -> "list[dict]":
    """#5350, reshaped by #5714 — the ``broker_session_id``-keyed sibling
    of :func:`process_for_agent`, identical contract (never matched by
    ``cwd``, matches across every ``sessions`` entry a process hosts)."""
    return [
        m for m in live_processes()
        if any(
            e.get("broker_session_id") == broker_session_id
            for e in m.get("sessions", [])
        )
    ]


def live_processes() -> "list[dict]":
    """Every currently-alive reyn process's own marker — read-only,
    reaping (deleting) any marker whose PID is confirmed no longer
    alive as a side effect of reading. ``[]`` when the directory does
    not exist yet (no reyn process has ever registered one).

    Reaping a DEAD-PID marker here is not "cleanup" in the sense #5226
    explicitly puts out of scope (killing/TTL-expiring a LIVE, still-
    abandoned session) — it only ever removes metadata about a process
    that :func:`~reyn.data.index.build_lock.pid_alive` has already
    confirmed does not exist, so the next read does not have to
    re-confirm the same negative.

    #5346: also reaps an orphaned ``.tmp`` staging file (:func:`register_process`'s
    write target BEFORE its atomic rename onto the real marker name) —
    the one shape that can leave one behind is the process dying between
    the ``.tmp`` write and the rename, a narrow window but not a zero
    one. A ``.tmp`` file is never trusted as a live marker regardless (the
    ``*.json`` glob below never matches it, and its own content is never
    read here — only its PID-shaped filename, since a still-mid-write
    file's content is exactly what must not be trusted); this is charter
    Q1's bounding subject for THAT one channel of "who stops it if it
    repeats" — a live, still-registering process's own ``.tmp`` is never
    touched (only a CONFIRMED-DEAD one's is), so this can never race the
    write it might belong to."""
    if not PROCESSES_DIR.is_dir():
        return []
    for tmp_path in sorted(PROCESSES_DIR.glob("*.json.tmp")):
        pid_str = tmp_path.name.removesuffix(".json.tmp")
        if not pid_str.isdigit() or pid_alive(int(pid_str)):
            continue
        # #5358 (architect, non-blocking note on #5359): deliberately no
        # process_marker_reaped event here — an orphaned .tmp means the
        # process died BEFORE ever completing registration (no real marker
        # was ever recorded), never that a registered process stopped. Its
        # own content is untrusted/unread by design (see this function's
        # own docstring above), so there is nothing to report losing.
        try:
            tmp_path.unlink()
        except OSError:
            pass
    result: "list[dict]" = []
    for path in sorted(PROCESSES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            # #5358: the marker itself is the only record that this PID
            # ever ran — the process that owned it never got to say it was
            # stopping (a graceful exit's own atexit handler would have
            # unlinked this file already; reaching this branch at all means
            # it did not). One event BEFORE the unlink below, carrying the
            # marker's own content plus this observation's own wall-clock
            # time (an UPPER BOUND on when the process actually stopped,
            # never the stop time itself — nothing here knows when between
            # the marker's last write and this read the process actually
            # died). This is not a crash-diagnosis mechanism: it does not
            # know WHY the process stopped (crash / SIGKILL / power loss /
            # OOM are indistinguishable from here), and a process that died
            # before ever reaching register_process() leaves no marker to
            # reap in the first place. What it closes is narrower and
            # structural: a stop that used to be perfectly silent (this
            # same reap, with no record before it) now has exactly one.
            #
            # lead-coder's TESTS-READ(B) BLOCK (#5359): the reaping PROCESS'S
            # own cwd (what emit_cli_event walks up from) has NOTHING to do
            # with the DEAD process's own project — PROCESSES_DIR is one
            # machine-wide directory, so `reyn doctor` run from project A can
            # reap a marker for a process that ran in project B, and (worse)
            # a caller running from a cwd inside NO project's `.reyn/` at all
            # made emit_cli_event's own best-effort "warn and return" fire
            # silently, reintroducing the exact silence this issue exists to
            # close. Fixed by resolving reyn_root from the DEAD marker's own
            # ``cwd`` field (the project the process actually ran in), never
            # the caller's — same `_find_reyn_dir` helper `emit_cli_event`
            # uses internally, called directly here via `emit_direct_event`.
            #
            # The whole block is best-effort, matching the unlink beneath it
            # and every other diagnostic-aid in this module (register_process
            # / _cleanup): an audit-emit failure (no `.reyn/` reachable from
            # the marker's own cwd, or any other error) must never block the
            # actual reap — leaving a marker permanently un-reaped because
            # its OWN diagnostic record failed to write would be a worse
            # regression than the record simply not existing this once.
            try:
                from reyn.core.events.events import _find_reyn_dir, emit_direct_event

                marker_cwd = data.get("cwd")
                reyn_dir = _find_reyn_dir(Path(marker_cwd)) if marker_cwd else None
                if reyn_dir is not None:
                    emit_direct_event(
                        "process_marker_reaped",
                        surface="cli",
                        reyn_root=reyn_dir,
                        track_audit_seq=False,
                        marker=data,
                        observed_at=time.time(),
                    )
            except Exception:
                logger.warning(
                    "process_registry: failed to emit process_marker_reaped "
                    "for pid %d (diagnostic-only, does not block the reap)",
                    pid, exc_info=True,
                )
            try:
                path.unlink()
            except OSError:
                pass
            continue
        result.append(data)
    return result


def read_process_markers() -> "list[dict]":
    """#5709 R3: the NON-DESTRUCTIVE sibling of :func:`live_processes` —
    every currently-present marker, dead-PID ones included, never
    reaping as a side effect.

    Why this exists (architect ruling, #5709, charter Q3 — "does the
    repair destroy the evidence"): :func:`live_processes` was always
    correct for what it answers ("who is alive right now" — reaping a
    confirmed-dead marker is part of THAT contract, not a defect). But
    once a marker can carry ``last_loop_beat_at`` (this same PR), a
    dead-but-not-yet-reaped marker becomes real EVIDENCE — the window
    between its last beat and whenever someone reads it. Two readers
    exist (``reyn doctor``, a broker health poll) and a marker can only
    be reaped once: whichever reads first would destroy the evidence
    the SECOND reader came to see. This function is what both now use —
    :func:`live_processes` itself is UNCHANGED (R3-1: reaping stays part
    of its own "who's alive" contract; this repo does not have a caller
    left that actually needs the reap side effect combined with a
    passive display read, so none is migrated onto this function against
    its will — see ``doctor.py``'s own updated ``_print_process_registry``
    for the one caller this PR does migrate).

    Includes dead-PID markers UNFILTERED (never checks
    :func:`~reyn.data.index.build_lock.pid_alive` at all) — a caller that
    wants only the alive subset re-derives it itself (this function
    never GUESSES liveness either way, matching this module's own
    "never fabricate" posture throughout). Skips a malformed/unreadable
    marker file the same way :func:`live_processes` does (best-effort,
    never raises over one bad file)."""
    if not PROCESSES_DIR.is_dir():
        return []
    result: "list[dict]" = []
    for path in sorted(PROCESSES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append(data)
    return result


# ---------------------------------------------------------------------------
# #5709: process-scoped loop-beat — "is this process's own turn loop still
# running", not merely "does the OS process still exist". See the module
# docstring's own WHY THIS EXISTS section for the narrower window this
# closes (started_at alone leaves a multi-hour death window, #5694).
# ---------------------------------------------------------------------------

#: The beat's own polling resolution — derivation, not a guess: the
#: longest "normal silence" a healthy turn can produce is one LLM call's
#: own bound (``chat.py``'s ``llm_call_seconds = 60.0``,
#: ``src/reyn/interfaces/cli/commands/chat.py``). 10s keeps the death
#: window well inside that bound without being so fine-grained the writes
#: themselves become a cost. Not a config key (architect ruling, #5709
#: R4): no operator has a reason to change it today — the day one does,
#: that need is the re-open trigger, not a guess made now.
_LOOP_BEAT_INTERVAL_S: Final[float] = 10.0


def record_loop_beat(
    *, pid: "int | None" = None, clock: "Callable[[], float] | None" = None,
) -> None:
    """The ONE write for ``last_loop_beat_at`` — called ONLY from
    :meth:`ProcessLoopBeatDriver.check`'s own tick, never from turn-path
    code (#5709 R2's own accept criterion: ``git grep 'record_loop_beat('
    -- src/`` must show no turn-path file as a caller).

    #5709 R6: read-modify-write with NO ``await`` between the read and
    the atomic replace — the same hazard :func:`record_process_identity`
    already avoids, named explicitly here because a beat tick racing an
    identity write (or vice versa) over the SAME marker file must never
    let one clobber the other's fields. Both are synchronous functions
    for exactly this reason; do not make either ``async``.

    Best-effort, matching this module's posture throughout: no marker
    (write failed, or this process never called :func:`register_process`)
    degrades to a silent no-op — a missed beat write must never raise
    into the driver's own tick loop.
    """
    if pid is None:
        pid = os.getpid()
    now = (clock or time.time)()
    marker_path = _marker_path(pid)
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    data["last_loop_beat_at"] = now
    try:
        tmp_path = _tmp_marker_path(pid)
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(marker_path)
    except OSError:
        logger.warning(
            "process_registry: failed to record loop beat for pid %d "
            "(diagnostic-only, does not block the caller)", pid, exc_info=True,
        )


class ProcessLoopBeatDriver:
    """#5709: the periodic driver behind ``last_loop_beat_at`` — a
    process-scoped singleton (never one per :class:`~reyn.runtime.
    session.Session`; see :func:`arm_process_loop_beat`, its own caller).

    ``clock``/``sleep`` are both injectable (CLAUDE.md: "a collaborator
    triggered only by its own timer may neither be faked nor waited for
    — give it an external drive"). Production uses real
    ``time.time``/``asyncio.sleep`` (the default for both); a test
    constructs this with a fake clock it advances directly and calls
    :meth:`check` itself — never a real sleep, and no wall-clock floor
    the test's own assertion depends on.
    """

    def __init__(
        self,
        *,
        interval_s: float = _LOOP_BEAT_INTERVAL_S,
        pid: "int | None" = None,
        clock: "Callable[[], float] | None" = None,
        sleep: "Callable[[float], Awaitable[None]] | None" = None,
    ) -> None:
        self._interval_s = interval_s
        self._pid = pid
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep

    def check(self) -> None:
        """One beat, right now — the externally-callable seam #5709 R2
        requires: a test (or, in production, this driver's own
        :meth:`run_forever` tick) calls this directly. No ``await``
        inside — see :func:`record_loop_beat`'s own R6 note."""
        record_loop_beat(pid=self._pid, clock=self._clock)

    async def run_forever(self) -> None:
        """The ONLY production caller of :meth:`check` — runs until
        cancelled (:class:`~reyn.runtime.tracked_tasks.TrackedTaskSet`'s
        own ``cancel_join`` disposition, via :func:`arm_process_loop_beat`).

        #5709 R2's own inversion trap: this must NOT be gated on "a turn
        is running" — the whole point is a beat that keeps landing
        WHILE a long turn is in flight (concurrent asyncio tasks; a
        healthy turn's own awaits give this task real scheduler
        opportunities without any special integration on the turn's own
        side), and BEFORE any turn has ever run at all (armed at the
        top of :meth:`Session.run`, before its own ``while
        run_one_iteration()`` loop starts)."""
        while True:
            await self._sleep(self._interval_s)
            self.check()


#: #5709 R5: a SEPARATE, process-scoped ``TrackedTaskSet`` — never a
#: ``Session``'s own ``self._background_tasks`` (that set's own
#: ``aclose()`` runs on THIS session's quiesce/rewind/shutdown, and a
#: beat tied to it would stop the moment ONE session in a
#: multi-session process ends, wrongly reporting every OTHER still-live
#: session's process as beat-less). Lazily constructed so importing this
#: module never requires a running event loop.
_beat_task_set: "TrackedTaskSet | None" = None
_beat_armed = False


def arm_process_loop_beat() -> None:
    """#5709: arm this process's own :class:`ProcessLoopBeatDriver`
    exactly once, no matter how many times — or from how many
    :class:`~reyn.runtime.session.Session` instances in this SAME
    process — this is called (R5's own accept criterion: 1 process + 2
    Sessions = 1 beater).

    Called from the top of :meth:`Session.run`, the earliest point THIS
    process's own turn loop actually starts running — later than
    :func:`register_process` (CLI startup, before any event loop
    exists) by necessity, not by choice; see this function's own
    call site for the disclosed seam choice.

    Idempotent and best-effort, matching this module's posture
    throughout: never raises into its caller over a diagnostic aid.
    """
    global _beat_task_set, _beat_armed
    if _beat_armed:
        return
    _beat_armed = True
    from reyn.runtime.tracked_tasks import TrackedTaskSet

    if _beat_task_set is None:
        _beat_task_set = TrackedTaskSet()
    driver = ProcessLoopBeatDriver()
    _beat_task_set.spawn(
        driver.run_forever(),
        name="process_loop_beat",
        disposition="cancel_join",
        appends_wal=False,
    )


def armed_beat_task_count() -> int:
    """#5709: the PUBLIC snapshot read for :func:`arm_process_loop_beat`'s
    own idempotency (R5's own accept criterion: 1 process + N Sessions =
    1 beater) — a test asserts through this, never the private
    ``_beat_task_set`` module global directly (this repo's own "a test
    must not depend on private state" policy). ``0`` before the first
    :func:`arm_process_loop_beat` call in this process."""
    return len(_beat_task_set) if _beat_task_set is not None else 0
