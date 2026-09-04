"""ProjectContextWatcher — read-only turn-boundary edit detection for the
project context file (AGENTS.md / REYN.md), #3787.

Deliberately NOT the #2073 HotReloader mechanism: that machinery re-reads
the IN-set (``.reyn/{mcp,cron,hooks,skills,pipelines,presentations}.yaml``)
because an LLM-op (``skill_install`` etc.) can request a reload — the IN-set
IS "the face the LLM is allowed to write". The project context file is
different in kind, not just format: ``RouterHostAdapter.get_project_context``
fences + scans its content before it reaches the system prompt (FP-0050/
#1822 S4b, EP3 Class A) — reyn itself treats this file as UNTRUSTED
operator-authored data. Wiring it into the LLM-writable IN-set would let the
LLM write its own untrusted-data source, an inversion the fence exists to
prevent. So this watcher only ever READS: it detects that the file changed
and emits an audit-event; it never re-reads the content into the live
prompt itself (architect ruling, #3787 — what to DO with a detected change,
notify-only vs. actually replace, is an open owner decision, ③).

mtime-based, not content-hash: this is a detection *signal*, not a cache
guard — the system prompt renderer is deterministic on ``project_context``
content, so re-reading the SAME content every turn would still hit the
prompt cache regardless of how the change is detected (architect
correction, #3787 — a hash-based re-render would guard nothing an mtime
check doesn't already surface, and would cost a full read every turn for
no benefit). One event per edit is the goal; the mtime comparison gives
that at the cost of one ``stat()`` per turn.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.core.events.events import EventLog


class ProjectContextWatcher:
    """Turn-boundary check: has the project context file's mtime moved since
    construction (or the last detected change)? Read-only — never touches
    the live ``project_context`` string a `Session` was built with.

    #5742 (architect ruling): every construction now names its own
    ``scope`` (``"project"`` or ``"agent"``) — a real class of consumer
    confusion, not a hypothetical one: both watchers already emitted the
    SAME ``project_context_changed`` kind, distinguishable only by
    sniffing ``path``'s shape (this file's own pre-#5742 comment,
    verbatim: "told apart from the project-wide one via the emitted
    ``path``"). Reyn prefers a typed discriminator over form-sniffing —
    see this module's own kind's row in ``docs/reference/runtime/
    events.md`` for the full rationale."""

    def __init__(
        self, *, path: "Path | None", events: "EventLog | None", scope: str,
    ) -> None:
        self._path = path
        self._events = events
        self._scope = scope
        self._last_mtime_ns = self._stat(path)

    @staticmethod
    def _stat(path: "Path | None") -> "int | None":
        if path is None:
            return None
        try:
            return path.stat().st_mtime_ns
        except OSError:
            # Deleted / unreadable since construction — treated as "no baseline
            # to compare against" rather than a change; a later re-appearance
            # will compare against this None and fire (arguably correct: the
            # file coming back IS an edit relative to what the session started
            # with).
            return None

    def check(self) -> bool:
        """Call at the turn boundary. Returns True (and emits exactly one
        ``project_context_changed`` P6 event) the first time the file's
        mtime differs from the last observed value; False otherwise
        (including: never configured, file missing both times, unchanged).

        Idempotent per edit: after firing, the new mtime becomes the
        baseline, so an unchanged file never fires again on subsequent
        turns."""
        if self._path is None:
            return False
        current = self._stat(self._path)
        if current == self._last_mtime_ns:
            return False
        self._last_mtime_ns = current
        if self._events is not None:
            self._events.emit(
                "project_context_changed", scope=self._scope, path=str(self._path),
            )
        return True
