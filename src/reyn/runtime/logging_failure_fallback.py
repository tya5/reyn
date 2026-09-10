"""A durable record of a `logging.Handler`'s own `emit()` failure, that does
NOT depend on the handler that just failed (#5989 ⑵).

#5989 PR1 (`run_textual_chat`'s widened `capture_stray_output` window)
closes the "goes to the operator's raw terminal" half of this issue -- but
closing that alone opens a NEW gap the owner's own three-question checklist
(CLAUDE.md, "does the repair destroy the evidence?") names directly:
`logging.Handler.handleError`'s own default behaviour, left alone, still
prints `--- Logging error ---` + a traceback to `sys.stderr` -- if PR1's
capture swallows that (as designed) and NOTHING else records it, the
handler's failure now vanishes with no trace at all, which is worse than
the original symptom (a garbled screen at least told the operator
SOMETHING broke).

The one thing this file's own destination must NOT depend on is the very
handler that is failing: reyn.log's own `RotatingFileHandler` re-logging
through itself (or through `logging` at all, which risks landing on that
SAME handler via propagation) proves nothing when THAT handler is what's
broken -- re-verified empirically in #5989's own comment thread (a strip
script: the underlying stream stays broken through a whole reentrant
retry, and the "durable" record ends up empty).

Reuses :class:`~reyn.runtime.diagnostic_snapshot.DiagnosticSnapshot` (the
SAME single-fd, always-overwritten, boundedness-by-SHAPE primitive
:class:`~reyn.runtime.loop_tripwire.StallDumpArm` already uses for the
stall dump -- #5978 ①'s own "this module's first caller" invitation to
reuse it) rather than inventing a second file-lifecycle mechanism. Same
pid-suffixed naming as :func:`~reyn.runtime.loop_tripwire.stall_dump_path`,
for the identical reason: N reyn processes sharing one `.reyn/logs/`
directory must not silently overwrite or interleave each other's records
(#5992's own finding, re-applied here rather than re-derived).

**Episode gating (#5989 ⑶, lead-coder ruling: "the same failure needs
recording only once per episode")**: the smallest shape that still answers
the question -- a plain "was the LAST recorded failure's own text
identical to this one" comparison. A REPEATING identical failure (the
common case: the same broken handler raising the same exception on every
subsequent record) collapses to one write per distinct occurrence rather
than one per record; a NEW, different failure (a different exception, or
the same handler recovering then breaking again with new content) is
still recorded -- `reset()`'d first, per :class:`DiagnosticSnapshot`'s own
truncate-timing rule (truncate for the NEXT write, never pre-emptively).
No cross-process/cross-restart state: a fresh process starts with no
prior "last recorded" text, so its first genuine failure is always
recorded regardless of what an earlier process (or an earlier run) last
saw -- this module holds no persisted state of its own, matching
`LoopTripwire`'s own per-instance (not per-file) episode memory.
"""
from __future__ import annotations

import logging.handlers
import os
import sys
import time
import traceback

from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot, diagnostic_snapshot


def handler_failure_dump_path(reyn_log_path: "str | None") -> "str | None":
    """The single-file destination for THIS PROCESS's handler-failure
    record — same directory `reyn.log` lives in, same pid-suffixed naming
    as :func:`~reyn.runtime.loop_tripwire.stall_dump_path` (see that
    function's own docstring for why the pid suffix is load-bearing, not
    decorative: N reyn processes attached to one workspace must not share
    one destination). `None` when there is no `reyn.log` path to sit
    beside, matching :meth:`DiagnosticSnapshot.open`'s own "no genuinely
    stable destination, no attempt" posture."""
    if reyn_log_path is None:
        return None
    from pathlib import Path

    return str(Path(reyn_log_path).with_name(f"handler_failure.{os.getpid()}.log"))


class FailureFallbackRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A `RotatingFileHandler` whose `handleError` ALSO records the
    failure to an independent destination (see module docstring) — a
    drop-in replacement for the plain `RotatingFileHandler`
    `_setup_interactive_logging` (`interfaces/cli/commands/chat.py`)
    already constructs; nothing else about this class's behaviour
    differs from the base.

    :meth:`set_fallback_path` must be called once, right after
    construction, by the installer — the same "declare the path
    directly" idiom :func:`~reyn.runtime.stall_trace.
    register_file_handler_path` already established, rather than making
    this class re-derive its own destination from `self.baseFilename`
    (which would couple this class to that attribute's own private
    shape). Uninstalled (`set_fallback_path` never called, or called with
    `None`) means this class behaves BYTE-IDENTICALLY to the plain base
    — every existing structural reader (`isinstance(handler, logging.
    FileHandler)` in `litellm_bootstrap.py`/`stall_trace.py`) still sees
    a `FileHandler`, since this class still IS one.

    Deliberately does NOT override `emit()` — `StreamHandler.emit()`
    already wraps its own body in `try/except Exception: self.handleError
    (record)` internally (stdlib, unchanged since first release); a
    subclass re-wrapping `emit()` itself would either never see the
    exception (the base class's OWN try/except already swallowed it
    before this subclass's wrapper's except clause could run) or
    double-handle it. `handleError` is the ONE correct override point —
    it is what the base class already calls, unconditionally, on every
    emit failure."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._fallback_snapshot: "DiagnosticSnapshot | None" = None
        self._fallback_path: "str | None" = None
        self._fallback_last_text: "str | None" = None

    def set_fallback_path(self, path: "str | None") -> None:
        self._fallback_path = path
        self._fallback_snapshot = None  # opened lazily, on first real failure

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib override name
        """#5989 ⑵: record this failure to the independent fallback
        destination, episode-gated (see module docstring), then defer to
        the base class for the REST of stdlib's own behaviour (the
        `sys.stderr` write #5989 PR1's own `capture_stray_output`
        widening already keeps off the real terminal during a TUI
        session — this method does not need to, and does not,
        short-circuit that base behaviour; the two fixes are independent
        layers, and neither assumes the other is present)."""
        try:
            self._record_fallback(record)
        except Exception:
            # A failure recording a failure must never mask the original
            # one, nor crash the logging call site — best-effort only,
            # matching every OTHER diagnostic writer in this codebase
            # (write_record, StallDumpArm's own callers).
            pass
        super().handleError(record)

    def _record_fallback(self, record: logging.LogRecord) -> None:
        if not self._fallback_path:
            return
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type is None:
            return  # handleError() was called outside an active exception -- nothing to record
        try:
            message = record.getMessage()
        except Exception as exc:  # the SAME "message vs args" failure class handleError itself guards
            message = f"<could not format: {type(exc).__name__}: {exc}>"
        text = (
            f"logger={record.name!r} level={record.levelname} "
            f"handler={type(self).__name__}\n"
            f"message={message!r}\n"
            + "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
        if text == self._fallback_last_text:
            return  # #5989 ⑶: same episode, already recorded -- collapse, don't re-write
        if self._fallback_snapshot is None:
            self._fallback_snapshot = diagnostic_snapshot(self._fallback_path)
            if self._fallback_snapshot is None:
                return  # no genuinely stable destination -- never attempt, matches StallDumpArm
        else:
            self._fallback_snapshot.reset()  # truncate for THIS new episode's own write
        if not self._fallback_snapshot.usable:
            return
        fd = self._fallback_snapshot.fd
        assert fd is not None  # `usable` already confirmed this (DiagnosticSnapshot's own contract)
        payload = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} #5989 handler failure\n{text}"
        try:
            os.write(fd, payload.encode("utf-8", errors="replace"))
        except OSError:
            return
        self._fallback_last_text = text
