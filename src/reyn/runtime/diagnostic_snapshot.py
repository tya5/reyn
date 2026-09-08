"""A single, always-overwritten diagnostic destination (#5977 ②, #5978 ①'s
general shape — "診断成果物に「構成からの有界性」を強制する").

#5978's own audit split diagnostic artifacts into two shapes by PURPOSE, not
by writer: a **snapshot** answers "what was stuck at the MOMENT something
went wrong" — always exactly one artifact, so boundedness comes from the
shape, no config surface needed — and a **stream** is a time-ordered record,
where overwriting would destroy the very thing it exists to keep, so IT
needs an explicit rate-limit or rotation instead. Rotating a snapshot (an
earlier #5977 ruling did exactly this, then self-corrected) or overwriting a
stream are both the SAME error in opposite directions: the bound stops
protecting the thing it was written for (#5973's own "a bound outlives the
reason it was written for" shape). This module is the snapshot half; see
#5978 for ``diagnostic_stream`` (not designed here — a separate PR, once its
five other callers are enumerated).

Stall dump (:class:`~reyn.runtime.loop_tripwire.StallDumpArm`) is this
module's first caller: the owner's own ``reyn.log`` measured 49% dump lines
(8,832 of 17,958) before #5977 ②, because the dump lived in a ROTATED,
APPENDED file answering a question ("what happened over time") that isn't
the one a stall dump actually answers ("what was stuck at the LAST stall") —
the file shape didn't match the artifact's own purpose.
"""
from __future__ import annotations

import os


class DiagnosticSnapshot:
    """One fd, held for this instance's whole lifetime, truncated to empty
    on open. No rotation, no generations, no size/count configuration —
    "a limit that can be set is a limit someone can raise" (architect,
    #5977): the only bound is the shape itself (always exactly one write).

    **The truncate-timing trap (architect, #5977)**: a caller that arms a
    timer-driven writer (e.g. ``faulthandler.dump_traceback_later``)
    against :attr:`fd` commits to the fd NUMBER at ARM time, not a live
    object — calling :meth:`reset` before that pending write actually
    lands would truncate out from under it, erasing a dump before it is
    ever written. :meth:`reset` must be called only AFTER the caller has
    observed that the PREVIOUS write landed, never preemptively on every
    re-arm.
    """

    def __init__(self, path: str, fd: int) -> None:
        self._path = path
        self._fd: "int | None" = fd

    @classmethod
    def open(cls, path: str) -> "DiagnosticSnapshot | None":
        """Open *path* truncated, creating its parent directory if
        needed. ``None`` on any ``OSError`` — a diagnostic that cannot be
        opened simply never arms, the same "no genuinely stable
        destination, no attempt" posture :class:`~reyn.runtime.
        loop_tripwire.StallDumpArm` already had for a missing log path."""
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        except OSError:
            return None
        return cls(path, fd)

    @property
    def fd(self) -> "int | None":
        """The live fd to write into, or ``None`` if opening/reopening
        last failed (:meth:`reset` sets this on a failed reopen; the
        instance stays unusable rather than silently resurrecting with a
        stale value)."""
        return self._fd

    @property
    def path(self) -> str:
        return self._path

    def reset(self) -> None:
        """Truncate and reopen for the NEXT write — see the class
        docstring's truncate-timing trap for why this must be called only
        after a PREVIOUS write is known to have landed, never
        preemptively. Sets :attr:`fd` to ``None`` on a failed reopen
        (parent directory removed mid-run, permissions changed, …) —
        every further write attempt against this instance then no-ops,
        matching :meth:`open`'s own "cannot open → never arms" posture."""
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        except OSError:
            self._fd = None

    def close(self) -> None:
        """Idempotent."""
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


def diagnostic_snapshot(path: str) -> "DiagnosticSnapshot | None":
    """Open (or create) *path* as a single, always-overwritten diagnostic
    destination. ``None`` when it cannot be opened — see
    :meth:`DiagnosticSnapshot.open`."""
    return DiagnosticSnapshot.open(path)
