"""#5989 symptom 3 (owner-hit — TUI unresponsive; ``reyn.log`` stayed at 0
lines for the whole session): the interactive CUI's own log redirect
(``chat._setup_interactive_logging``) installs a ``RotatingFileHandler``
partway through startup — anything logged BEFORE that call (interpreter
init, ``argparse``, :func:`~reyn.runtime.process_registry.register_process`,
the import tree) has no handler to reach yet. Python's own fallback for
"a record with no handler anywhere in the hierarchy" is ``logging.
lastResort`` — a bare ``StreamHandler(sys.stderr)`` at ``WARNING`` — so a
pre-redirect record goes straight to stderr and is gone: once the real file
handler installs later, there is no mechanism that goes back and writes it
into ``reyn.log``.

lead-coder's own explicit scoping (issue #5989 comment thread): this closes
the HOLE ("a record emitted here is unrecoverable"), not symptom 3 itself
— the reported "0 lines for 5 minutes of live usage" is a different order
of magnitude than this narrow startup window and still needs an owner
real-machine trace to explain.

## Industry precedent (checked before designing, per owner's standing
directive)

``logging.handlers.MemoryHandler`` is the stdlib's own "buffer records,
flush to a target once one exists" primitive — the exact shape this needs
(buffer everything from process start, replay it into the real
``RotatingFileHandler`` the moment :func:`~reyn.interfaces.cli.commands.
chat._setup_interactive_logging` installs it). Reused, not reinvented,
with one deliberate deviation:

``MemoryHandler.flush()``'s own docstring states "the record buffer is
only cleared if a target has been set" — verified by reading ``cpython``'s
own source (3.13): with no target, ``BufferingHandler.emit()`` still
unconditionally does ``self.buffer.append(record)`` (a plain ``list``)
forever — ``shouldFlush()``/``flush()`` firing at ``capacity`` does
NOTHING to trim it without a target already attached. A process that never
reaches ``_setup_interactive_logging`` at all (``--cui``/non-TTY runs,
which never install ANY file handler by design; or a crash before that
call) would otherwise buffer records without bound for its entire life —
six-questions #5 ("what does this accumulate, who bounds it") answered
"nothing, forever" is not an answer. :class:`_BoundedEarlyBuffer` below
replaces the plain list with a ``collections.deque(maxlen=capacity)`` —
oldest record silently dropped once full, a real, stated ceiling
independent of whether a target ever attaches.

## What happens on each of the two paths

- **A target attaches** (the interactive CUI installs its real handler):
  every buffered record (up to ``capacity``, oldest-dropped beyond that)
  replays into it once, in order, then the buffer is cleared and never
  buffers again — see :meth:`_BoundedEarlyBuffer.replay_into`.
- **No target ever attaches** (``--cui``/non-TTY, or an early crash): the
  bounded buffer just sits capped, doing nothing further, for the rest of
  the process's life — bounded memory, no behavioural change to anything
  else. The mirrored ``StreamHandler(sys.stderr)`` installed alongside it
  (see :func:`install`) keeps stderr visibility IDENTICAL to what
  ``logging.lastResort`` already provided before this change (same level,
  same destination) — this module only makes that fallback explicit
  early enough to also feed the buffer; it does not add or remove a
  second copy of anything a non-interactive run already showed.

``logging.captureWarnings(True)`` is armed in the SAME early window (not
only later, inside ``_setup_interactive_logging`` as before) — a bare
``warnings.warn(...)`` before the real handler installs is the same
unrecoverable-loss shape as a logging call, and #4362 already established
that raw ``warnings.warn`` must not corrupt the interactive UI either.
Disclosed cost, not hidden: this changes ``warnings.warn``'s own OUTPUT
FORMAT (via ``py.warnings``'s logger + this module's `Formatter`, not
``warnings.showwarning``'s own ``path:line: Category: message`` shape) for
every ``reyn`` invocation from process start, not only the interactive
one — the same format change #4362 already accepted for the
post-handler-install window, now starting earlier. Calling it twice
(here, and again inside ``_setup_interactive_logging``) is idempotent —
``logging.captureWarnings`` only (re)installs the same monkeypatch.
"""
from __future__ import annotations

import logging
import sys
from collections import deque
from logging.handlers import MemoryHandler

#: Matches the field name/shape #5873 already established for the real
#: RotatingFileHandler's own bound ("a limit that can be set is a limit
#: someone can raise" does not apply here — this is a fixed, small ceiling
#: on a startup-only window, not an operator-configurable log destination).
_DEFAULT_CAPACITY = 200


class _BoundedEarlyBuffer(MemoryHandler):
    """A :class:`~logging.handlers.MemoryHandler` with NO auto-flush and a
    hard-bounded buffer — see this module's own docstring for why the
    stock class's "unbounded without a target" gap matters here.
    ``target`` is always ``None`` at construction; the only way records
    leave the buffer is :meth:`replay_into`, called explicitly once a
    real handler exists."""

    def __init__(self, capacity: int) -> None:
        super().__init__(capacity=capacity, flushLevel=logging.CRITICAL + 1, target=None)
        # Replaces BufferingHandler's own plain `list` with a bounded
        # deque -- appending past `capacity` silently drops the OLDEST
        # entry instead of growing forever (see module docstring).
        # ignore[assignment] below: BufferingHandler types `self.buffer`
        # as `list`; a `deque` stays a Sequence everywhere the base
        # class's own code (close()'s flush()) reads it.
        self.buffer: "deque[logging.LogRecord]" = deque(maxlen=capacity)  # type: ignore[assignment]

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        """Never auto-flush -- ``target`` is always ``None`` here, so the
        base class's own ``flush()`` would no-op anyway (module
        docstring); :meth:`replay_into` is the only exit."""
        del record
        return False

    def replay_into(self, target: logging.Handler) -> None:
        """Hand every currently-buffered record to *target*, in order,
        then clear the buffer. Idempotent — calling this again with an
        already-empty buffer is a no-op, so a caller does not need to
        track whether it already replayed once."""
        self.acquire()
        try:
            for record in self.buffer:
                target.handle(record)
            self.buffer.clear()
        finally:
            self.release()


_installed: "_BoundedEarlyBuffer | None" = None


def install(capacity: int = _DEFAULT_CAPACITY) -> _BoundedEarlyBuffer:
    """Install the bounded early buffer (plus a stderr mirror matching
    ``logging.lastResort``'s own level/destination) on the root logger, as
    early in process startup as this can be called. Idempotent — a second
    call returns the SAME instance already installed, never double-installs.

    Call this as the very first statement of ``reyn``'s own CLI entry
    point (:func:`reyn.interfaces.cli.main`) — before anything else that
    could log, including argument parsing and process registration."""
    global _installed
    if _installed is not None:
        return _installed
    root = logging.getLogger()
    mirror = logging.StreamHandler(sys.stderr)
    mirror.setLevel(logging.WARNING)
    root.addHandler(mirror)
    buffer = _BoundedEarlyBuffer(capacity)
    buffer.setLevel(logging.WARNING)
    root.addHandler(buffer)
    logging.captureWarnings(True)
    _installed = buffer
    return buffer


def get_installed() -> "_BoundedEarlyBuffer | None":
    """The buffer :func:`install` armed, or ``None`` if this process never
    called it (e.g. a test constructing ``chat._setup_interactive_logging``
    directly, never having gone through the real CLI entry point)."""
    return _installed


def _reset_for_tests() -> None:
    """Test-only: clears the module-level singleton so a test can call
    :func:`install` again as if starting fresh. Never called from
    production code."""
    global _installed
    _installed = None
