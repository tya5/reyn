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
    from starting is worse than the diagnosis it offers")."""
    pid = os.getpid()
    marker = {
        "pid": pid,
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "subcommand": subcommand,
        "started_at": time.time(),
    }
    try:
        PROCESSES_DIR.mkdir(parents=True, exist_ok=True)
        _marker_path(pid).write_text(json.dumps(marker), encoding="utf-8")
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
    re-confirm the same negative."""
    if not PROCESSES_DIR.is_dir():
        return []
    result: "list[dict]" = []
    for path in sorted(PROCESSES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        result.append(data)
    return result
