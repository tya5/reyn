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
overrides ``emit()`` to stop accepting new records once ``capacity`` is
reached, instead of growing forever.

Not a ``MemoryHandler`` SUBCLASS, in the end, despite it being the
precedent that led here: the semantics that matter for this use
(refuse-past-capacity, count what was refused, no ``target``/``flush()``
concept at all) replace enough of ``MemoryHandler``'s own state
(``buffer``, ``target``) that inheriting from it would leave its
UNoverridden methods (``close()``'s own ``flushOnClose`` path calls
``self.flush()``, which reads attributes this class no longer sets)
silently broken rather than merely unused. Subclassing plain
``logging.Handler`` instead keeps every method on this class doing
exactly what its own code says, at the cost of re-declaring `acquire`/
`release` usage explicitly (inherited from ``logging.Handler`` itself,
unchanged).

## Which end drops on overflow — and why (lead-coder review of this PR's
first version, caught live)

The first version used a ``collections.deque(maxlen=capacity)``, which
drops the OLDEST record once full — plausible-looking (it is the shape
most "ring buffer" examples default to), but backwards for what THIS
buffer exists to save: the motivating case is the record emitted closest
to process start (the very first warning after interpreter init), and a
buffer that drops FROM THE FRONT on overflow discards exactly that record
the moment ``capacity`` is exceeded — recreating the original bug's own
shape (the earliest record lost) via the fix meant to prevent it.

Fixed: :meth:`_BoundedEarlyBuffer.emit` stops ACCEPTING new records once
``capacity`` is reached — the buffer keeps the OLDEST ``capacity`` records
(the ones closest to process start, the actually-motivating case) and
counts how many later ones it had to refuse, rather than silently
discarding either end. That count is not lost either: :meth:`replay_into`
appends one synthetic ``WARNING`` record stating exactly how many were
dropped, so a ``reyn.log`` reader can tell "0 records lost" from "N
records lost" instead of the two reading identically (lead-coder review:
the durable, cross-session-readable surface is ``reyn.log`` itself —
#5977's own measured incident — not the stderr mirror, which is only
this ONE process's own terminal).

## What happens on each of the two paths

- **A target attaches** (the interactive CUI installs its real handler):
  every buffered record (the earliest ``capacity`` of them) replays into
  it once, in order, followed by the dropped-count record if any were
  refused, then the buffer is cleared and never buffers again — see
  :meth:`_BoundedEarlyBuffer.replay_into`.
- **No target ever attaches** (``--cui``/non-TTY, which never installs
  ANY file handler by design; or an early crash before
  ``_setup_interactive_logging`` runs): an :mod:`atexit` hook (armed by
  :func:`install`) dumps whatever is still buffered straight to stderr,
  ONCE, at process exit — see :func:`_flush_to_stderr_at_exit`.

## A real regression, caught by owner review, and its fix (#6043)

The first version of this module installed a SECOND stderr handler
(``mirror``) unconditionally alongside the buffer, reasoning it "matches
``logging.lastResort``'s own level/destination, so it doesn't change
current visibility." That reasoning does not hold, and it broke the
interactive CUI on a real machine (owner report, #6043 — "reyn.log に
残せば良いのにわざわざ stderr 使ってるの？", a raw ``logging`` line
appearing between the sent-queue and the input box):

- ``logging.lastResort`` only fires when a logger's effective handler
  chain is EMPTY — the moment ANY handler attaches to root (the buffer
  itself, with no mirror at all), ``lastResort`` already stops firing on
  its own. The mirror did not "keep the same destination active"; it
  added a SECOND, unconditional one that fires on every single record,
  not only when nothing else would have caught it.
- The buffer's own purpose was "replay into ``reyn.log`` later" — once
  that replay lands, writing the SAME records to stderr too is not
  preserving prior behaviour, it is duplicating output, live, for the
  entire time the interactive TUI owns the terminal (every WARNING+
  record from then on, not just the pre-handler window this module
  exists to cover).

Fixed: no mirror handler at all. The ONLY path records ever reach stderr
through THIS module is the exit-time fallback above, and only when
:meth:`_BoundedEarlyBuffer.replay_into` never ran at all — the interactive
path (where ``replay_into`` DOES run) never sees it.

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

## Capacity

``_DEFAULT_CAPACITY = 200`` is an ESTIMATE, not a measurement (owner's
standing directive asks for the structural reasoning when a real count
isn't taken, not a fabricated one): the window this buffer covers runs
from interpreter start through ``reyn.interfaces.cli.main``'s own
argument parsing and :func:`~reyn.runtime.process_registry.
register_process` call, before ``_setup_interactive_logging`` — a
handful of statements and one import tree, not a loop that could emit
records proportional to input size. 200 is generous headroom over what
that shape could structurally produce, not a tight fit. If it is wrong
in either direction, the failure mode is no longer silent either way: a
capacity too LOW still keeps the earliest records (the motivating case)
and reports exactly how many later ones it refused (this section's own
fix above); a capacity too HIGH costs a few hundred extra ``LogRecord``
objects for a few milliseconds of process life — never observed, never
measured, not worth measuring.
"""
from __future__ import annotations

import atexit
import logging
import sys

#: See this module's own "Capacity" section for why this is an estimate,
#: not a measurement, and why being wrong in either direction stays safe.
_DEFAULT_CAPACITY = 200


class _BoundedEarlyBuffer(logging.Handler):
    """Buffers up to ``capacity`` records — the EARLIEST ones, the actual
    motivating case (see module docstring's own "which end drops"
    section) — refusing (not silently overwriting) anything past that,
    while counting how many were refused. The only way records leave the
    buffer is :meth:`replay_into`, called explicitly once a real handler
    exists; there is no target to flush into before that, so nothing
    auto-flushes."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._capacity = capacity
        self._records: "list[logging.LogRecord]" = []
        self._dropped = 0
        #: True once :meth:`replay_into` has run at least once — the
        #: exit-time fallback (:func:`_flush_to_stderr_at_exit`) checks
        #: this to stay a no-op on the interactive path, where a real
        #: replay already happened.
        self.replayed = False

    @property
    def kept_messages(self) -> "tuple[str, ...]":
        """The currently-buffered records' own formatted messages, in
        order — a public, read-only surface so a caller (a test
        included) does not need to reach into ``_records`` directly."""
        self.acquire()
        try:
            return tuple(r.getMessage() for r in self._records)
        finally:
            self.release()

    @property
    def dropped_count(self) -> int:
        """How many records this instance has refused past ``capacity``
        since the last :meth:`replay_into` (or since construction, if
        none happened yet)."""
        self.acquire()
        try:
            return self._dropped
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            if len(self._records) < self._capacity:
                self._records.append(record)
            else:
                self._dropped += 1
        finally:
            self.release()

    def replay_into(self, target: logging.Handler) -> None:
        """Hand every currently-buffered record to *target*, in order,
        then — if any records were refused past ``capacity`` — one
        synthetic ``WARNING`` stating exactly how many, so a ``reyn.log``
        reader can tell "0 lost" from "N lost" rather than the two
        looking identical. Clears the buffer and its drop counter
        afterward. Idempotent — calling this again with an already-empty
        buffer and zero drops is a no-op."""
        self.acquire()
        try:
            for record in self._records:
                target.handle(record)
            if self._dropped:
                dropped_logger = logging.getLogger(__name__)
                dropped_record = dropped_logger.makeRecord(
                    dropped_logger.name, logging.WARNING, __file__, 0,
                    "%d early log record(s) were dropped before the "
                    "interactive CUI's own log file existed (buffer "
                    "capacity=%d exceeded)",
                    (self._dropped, self._capacity), None,
                )
                target.handle(dropped_record)
            self._records.clear()
            self._dropped = 0
            self.replayed = True
        finally:
            self.release()


_installed: "_BoundedEarlyBuffer | None" = None


def _flush_to_stderr_at_exit(buffer: _BoundedEarlyBuffer) -> None:
    """#6043: the ``atexit`` fallback for the ONE real path where
    :meth:`_BoundedEarlyBuffer.replay_into` never runs — ``--cui``/
    non-TTY invocations (which never call ``chat._setup_interactive_
    logging`` at all, by design) and an early crash before that call.
    A no-op if ``replay_into`` already ran (the interactive path) —
    checked first, so this never duplicates output there."""
    if buffer.replayed:
        return
    stderr_handler = logging.StreamHandler(sys.stderr)
    buffer.replay_into(stderr_handler)


def install(capacity: int = _DEFAULT_CAPACITY) -> _BoundedEarlyBuffer:
    """Install the bounded early buffer on the root logger, as early in
    process startup as this can be called. Idempotent — a second call
    returns the SAME instance already installed, never double-installs.

    #6043: NO stderr mirror handler — see this module's own "A real
    regression" section for why one existed here before and why it was
    wrong. The only path buffered records reach stderr through this
    module is :func:`_flush_to_stderr_at_exit`, armed below, which only
    acts if :meth:`_BoundedEarlyBuffer.replay_into` never ran.

    Call this as the very first statement of ``reyn``'s own CLI entry
    point (:func:`reyn.interfaces.cli.main`) — before anything else that
    could log, including argument parsing and process registration."""
    global _installed
    if _installed is not None:
        return _installed
    root = logging.getLogger()
    buffer = _BoundedEarlyBuffer(capacity)
    buffer.setLevel(logging.WARNING)
    root.addHandler(buffer)
    logging.captureWarnings(True)
    _installed = buffer
    atexit.register(_flush_to_stderr_at_exit, buffer)
    return buffer


def get_installed() -> "_BoundedEarlyBuffer | None":
    """The buffer :func:`install` armed, or ``None`` if this process never
    called it (e.g. a test constructing ``chat._setup_interactive_logging``
    directly, never having gone through the real CLI entry point)."""
    return _installed


def _reset_for_tests() -> None:
    """Test-only: clears the module-level singleton AND unregisters the
    ``atexit`` fallback :func:`install` armed, so a test can call
    :func:`install` again as if starting fresh. Without the ``atexit.
    unregister`` call, every test that calls :func:`install` would leave
    its own fallback registered — accumulating for the rest of the
    pytest process's life, each one referencing a stale buffer instance,
    all firing (harmlessly, but pointlessly) at the real pytest process's
    own exit. ``atexit.unregister`` matches by the callable itself
    (:func:`_flush_to_stderr_at_exit`), not by the *buffer* argument it
    was registered with, so this removes every pending registration this
    module has ever made in THIS process, not only the most recent one —
    correct here since a test-only reset should leave none behind at
    all. Never called from production code."""
    global _installed
    atexit.unregister(_flush_to_stderr_at_exit)
    _installed = None
