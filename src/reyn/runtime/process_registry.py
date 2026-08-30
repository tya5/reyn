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

import atexit
import json
import logging
import os
import time
from pathlib import Path
from typing import Final

from reyn.data.index.build_lock import pid_alive

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
        # #5350: absent (never guessed) until a later, more-informed call
        # to :func:`record_process_identity` sets one or both — this
        # process's own agent_name/broker_session_id are usually not yet
        # resolved at THIS call site (register_process runs at CLI
        # startup, before Session construction).
        "agent_name": None,
        "broker_session_id": None,
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


def record_process_identity(
    *,
    agent_name: "str | None" = None,
    broker_session_id: "str | None" = None,
    pid: "int | None" = None,
) -> None:
    """#5350 — a process records its OWN identity onto its already-written
    marker, once it actually knows it (``register_process`` runs at CLI
    startup, before that is usually resolved). Never a guess: the caller
    is always the process itself (or code acting on its behalf) stating
    a fact it already has — never a lookup, never derived from ``cwd``.

    Only fields the caller actually passes are updated (``None`` here
    means "nothing new to say", not "clear the existing value" — a
    second caller that only knows ``broker_session_id`` must not erase
    an ``agent_name`` a first caller already recorded). Best-effort,
    matching this module's own posture throughout: no marker (the
    process never called :func:`register_process`, or its write failed)
    degrades to a silent no-op, never an exception that could interrupt
    the caller's own real work over a diagnostic aid.
    """
    if pid is None:
        pid = os.getpid()
    marker_path = _marker_path(pid)
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if agent_name is not None:
        data["agent_name"] = agent_name
    if broker_session_id is not None:
        data["broker_session_id"] = broker_session_id
    try:
        tmp_path = _tmp_marker_path(pid)
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(marker_path)
    except OSError:
        logger.warning(
            "process_registry: failed to record identity for pid %d "
            "(diagnostic-only, does not block the caller)", pid, exc_info=True,
        )


def process_for_agent(agent_name: str) -> "list[dict]":
    """#5350 — every currently-alive process whose OWN recorded
    ``agent_name`` matches *agent_name* (via :func:`live_processes`,
    which already reaps dead-PID markers as it reads). NEVER matched by
    ``cwd`` — the architect-named incident this exists to prevent: an
    unrelated process (a shell, an editor) sharing a directory with a
    reyn agent is not that agent, and must never be returned here.
    Ordinarily a list of 0 or 1 — more than one means two processes
    both recorded the SAME ``agent_name`` (a caller error upstream, not
    something this function corrects or hides)."""
    return [m for m in live_processes() if m.get("agent_name") == agent_name]


def process_for_broker_session(broker_session_id: str) -> "list[dict]":
    """#5350 — the ``broker_session_id``-keyed sibling of
    :func:`process_for_agent`, identical contract (never matched by
    ``cwd``)."""
    return [
        m for m in live_processes() if m.get("broker_session_id") == broker_session_id
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
