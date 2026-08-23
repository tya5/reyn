"""#5168: while a Textual app owns the screen, ``sys.stdout``/``sys.stderr``
are the RENDERER's own — anything else writing to them corrupts the live
region. Measured live (e2e-coder, real tmux TTY): a ``warnings.warn()`` fired
mid-session left a garbled fragment durably stuck in the input composer's
own rendered line (survived multiple ``capture-pane`` reads seconds apart,
not a single transient redraw frame).

Architect ruling (issuecomment-5384424061): the problem is not "what to do
about warnings" — it is "who owns these streams while the app owns the
screen." Two tty owners cannot coexist. Fixing only ``warnings`` (e.g.
``logging.captureWarnings``, already installed for the CLI's own log
redirect — see ``interfaces/cli/commands/chat.py``'s ``_setup_interactive_
logging``) closes the ONE observed trigger but not the general path: a
third party can bypass ``warnings`` entirely with a bare
``print(..., file=sys.stderr)``, and the next one will corrupt the screen
the same way (the exact #5164-doesn't-close-this shape e2e-coder's own
report named).

**Scope: the stream, not the writer.** ``capture_stray_output`` swaps
``sys.stdout``/``sys.stderr`` themselves for the duration the app owns the
screen. ``warnings.warn``'s default ``showwarning`` implementation writes to
``sys.stderr`` looked up LIVE at call time (not bound at import), so it
rides this swap automatically -- no separate ``warnings`` handling needed on
this path (``logging.captureWarnings`` stays installed and correct for the
CLI's OTHER, non-TUI callers of ``_setup_interactive_logging`` -- this
module does not touch that).

**Destination: reyn's own logging, never dropped.** Every captured write is
logged via ``logging.getLogger(__name__)`` at WARNING level -- routed
through whatever handler the interactive CLI already installed (a
``.reyn/logs/reyn.log`` FileHandler, so ``--verbose``/log-tooling still sees
it) rather than a raw, separately-invented file. ``filterwarnings("ignore")``
is explicitly NOT an option here (architect ruling) -- that is the #5167
shape this whole arc has been closing all night ("declared/received, never
honored, never said"), generalized to output instead of a hook declaration.

**Visibility: one line, never per-write.** A captured write bumps
``StrayOutputStats.count`` and posts a ``StrayOutputCaptured`` message (NOT
a direct method call -- posting is Textual's thread-safe primitive, and a
stray write can originate from a background thread a third-party library
owns, not only the app's own event loop) so the App can fold it into the
ALREADY-EXISTING status-line refresh (``chrome.status_line_text``'s new
``diagnostics_count``/``diagnostics_log_path`` params) -- one row, updated
in place, never a line printed per occurrence (that repeated-line spam IS
the corruption this closes) and never a modal (the ruling's own explicit
"not a thing that stops operation").

**Restore: unconditional, including a crash mid-run.** ``capture_stray_
output`` is a context manager; ``__exit__`` runs on every exit path a
``with`` block has -- a normal return, a Textual-internal exception, a
KeyboardInterrupt during the run -- so an operator's terminal is never left
mid-swap. The two assignments in ``__enter__`` are adjacent with no I/O or
branching between them, so the only window a crash could land in is the
attribute-store bytecode itself, which cannot raise -- there is no state
between "neither swapped" and "both swapped" for an exception to observe.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from textual.message import Message
from textual.message_pump import MessagePump

_log = logging.getLogger(__name__)


@dataclass
class StrayOutputStats:
    """Live-mutated counter :func:`capture_stray_output` hands to its
    caller. One instance per capture window (per TUI run) -- the App reads
    ``.count`` at each status-line refresh; nothing here is Textual-specific,
    so this dataclass alone is fully unit-testable with no App/widget
    involved."""

    count: int = 0


class StrayOutputCaptured(Message):
    """#5168: posted once per non-empty captured write. Carries no payload
    deliberately -- a handler re-reads the live :class:`StrayOutputStats`
    count rather than trusting a value that might already be stale by the
    time Textual's message queue delivers it (several writes can be posted
    before the App's handler runs; each handler invocation should reflect
    the CURRENT total, not the count at post time)."""


class _CapturingStream:
    """A write-only file-like object that logs everything it receives
    instead of letting it reach the real terminal.

    Not a general-purpose stream proxy -- ``write`` is the only method a
    ``print(...)``/``warnings.showwarning`` call actually needs; ``flush``
    is a no-op (nothing buffered to flush) and ``isatty`` returns ``False``
    (a library checking "am I on a real terminal before emitting ANSI
    codes" must see "no" here -- this is not one)."""

    def __init__(self, stats: StrayOutputStats, poster: "MessagePump", stream_name: str) -> None:
        self._stats = stats
        self._poster = poster
        self._stream_name = stream_name

    def write(self, s: str) -> int:
        if s and s.strip():
            self._stats.count += 1
            _log.warning("stray %s captured during TUI run: %s", self._stream_name, s.rstrip("\n"))
            try:
                self._poster.post_message(StrayOutputCaptured())
            except Exception:  # noqa: BLE001 — a message-post fault must never crash the write() caller
                pass
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class capture_stray_output:  # noqa: N801 — lowercase, contextlib-style naming (mirrors stdlib's own `contextlib.redirect_stdout`)
    """Context manager: swap ``sys.stdout``/``sys.stderr`` to
    :class:`_CapturingStream` instances for the duration ``poster`` (the
    running :class:`~textual.app.App`) owns the screen. See the module
    docstring for the full rationale.

    ``poster`` is any ``MessagePump`` (Textual's ``App``/``Widget`` base --
    ``post_message`` lives there, not on ``App`` specifically) -- typed as
    the real Textual base rather than importing
    ``reyn.interfaces.inline.textual_chat.app`` to avoid a circular import
    (this module is imported BY ``app.py``)."""

    def __init__(self, poster: "MessagePump") -> None:
        self.stats = StrayOutputStats()
        self._poster = poster
        self._orig_stdout: "object | None" = None
        self._orig_stderr: "object | None" = None

    def __enter__(self) -> StrayOutputStats:
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout = _CapturingStream(self.stats, self._poster, "stdout")
        sys.stderr = _CapturingStream(self.stats, self._poster, "stderr")
        return self.stats

    def __exit__(self, exc_type, exc, tb) -> None:
        # Unconditional restore -- see the class docstring's own "Restore"
        # paragraph for why this is safe even mid-crash.
        if self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
        if self._orig_stderr is not None:
            sys.stderr = self._orig_stderr
