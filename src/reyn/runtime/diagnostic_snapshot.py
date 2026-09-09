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

    #6000 ②/architect: :attr:`fd` and :attr:`usable` are TWO SEPARATE
    facts, not one overloaded ``None``. The original design used
    ``fd is None`` to mean both "never opened / already closed" AND
    "reset() just failed, don't write here" — the moment ``reset()``
    stopped ever releasing the fd number (the whole point of ②), those
    two facts diverged: a failed ``reset()`` leaves a real, still-open
    fd (the number is never released, on purpose) that is nonetheless
    NOT SAFE to arm against again (stale content, or an orphaned inode)
    — ``fd is None`` can no longer answer "may I arm/write." This repo
    has already recorded "``None`` standing for two facts fails open" as
    a recurring shape (architect, reviewing this PR) — this is its 3rd
    instance. :attr:`usable` answers the arming question; :attr:`fd`
    answers "what number, if any, do I have."
    """

    #: Narrower than the built-in ``open(path, "a")``'s own default
    #: (``0o666``) — a stall/diagnostic dump can contain a path or argv
    #: (#6000, architect review), so this deliberately NARROWS rather
    #: than merely matching the historical default. ``os.open``'s own
    #: default (``0o777``, masked by umask) would be wider still.
    _MODE = 0o600

    def __init__(self, path: str, fd: int) -> None:
        self._path = path
        #: Never ``None`` while this instance is otherwise alive — see
        #: the class docstring. Only :meth:`close` releases it, in the
        #: one case where releasing the number really is the right
        #: outcome (an intentional, final teardown).
        self._fd: "int | None" = fd
        #: False after a `reset()` failure (or after `close()`) — "may
        #: this snapshot be armed or written to." See the class
        #: docstring for why this is a SEPARATE fact from `fd`.
        self._usable = True

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
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, cls._MODE)
        except OSError:
            return None
        return cls(path, fd)

    @property
    def fd(self) -> "int | None":
        """The fd number this instance holds, or ``None`` if the initial
        :meth:`open` never succeeded (no instance exists in that case,
        so this is really "never constructed"), or :meth:`close` already
        ran. #6000 ②: NOT ``None`` after a failed :meth:`reset` — that
        fd is still open and still this number, just not
        :attr:`usable`. A caller deciding whether to ARM must check
        :attr:`usable`, never ``fd is None`` — see the class docstring."""
        return self._fd

    @property
    def usable(self) -> bool:
        """Whether this snapshot may still be armed or written to right
        now. ``False`` after a `reset()` failure (see that method's own
        docstring) or after `close()`. Composed with ``fd is not None``
        so a not-yet-opened or already-closed instance also reads as
        unusable without a second check."""
        return self._usable and self._fd is not None

    @property
    def path(self) -> str:
        return self._path

    def points_at_current_file(self) -> bool:
        """Whether :attr:`fd` still points at the file CURRENTLY at
        :attr:`path` (inode identity) — ``False`` if something external
        deleted or moved it out from under this instance (a cleanup tool
        touching the file, an operator ``rm``ing it, …) since this fd was
        last opened. ``False`` when :attr:`usable` is already ``False``
        too, matching every other "nothing usable" case here.

        Not a rotation check — this module drives no rotation of its own
        (see the module docstring) — but the SAME question a rotation
        check would ask, generalized to "is my fd still findable via the
        path anyone else would read," regardless of WHY it stopped being
        true. Left unanswered, a caller that keeps re-arming against a
        now-orphaned fd writes dumps nobody can ever read again — never
        with an error, since the write itself still succeeds — the exact
        silent-loss shape lead-coder flagged reviewing #5988."""
        if not self.usable:
            return False
        assert self._fd is not None  # `usable` already confirmed this
        try:
            return os.stat(self._path).st_ino == os.fstat(self._fd).st_ino
        except OSError:
            return False

    def reset(self) -> None:
        """Truncate for the NEXT write — see the class docstring's
        truncate-timing trap for why this must be called only after a
        PREVIOUS write is known to have landed, never preemptively.

        #6000 ②/architect (superseding #5998's own "the caller must
        disarm first" contract, kept below as HISTORY): the fd NUMBER
        this method exposes is now NEVER released back to the OS across
        a `reset` — UNCONDITIONALLY, including on failure, not only on
        the two success paths. (lead-coder + architect review of this
        PR's own first version caught two real gaps here: the common-case
        `except` branch called `os.close(self._fd)` on a truncate
        failure — releasing the number — and the external-change branch's
        own `except` branch set `self._fd = None` on a reopen failure
        with NO close at all — a genuine LEAK, the fd stays open but this
        instance forgets it. "Rare" was not an answer to either: ② exists
        to replace "usually nobody forgets" with "cannot happen," and a
        failure path is exactly the code a discipline-based guarantee
        quietly stops covering.)

        Fixed shape — ALL THREE real outcomes leave :attr:`fd` exactly
        as it was, an open, unreleased fd:

          - common case, success (:meth:`points_at_current_file` still
            true): truncated IN PLACE via ``os.ftruncate`` + ``os.lseek``,
            never closed at all.
          - external-change case, success (something deleted/replaced
            *path* since this fd was opened): a NEW fd is opened at
            *path*, then :func:`os.dup2` onto :attr:`fd`'s OWN number —
            ``dup2`` is POSIX-atomic, so that number is NEVER momentarily
            unused, the decisive difference from "close old, then open
            new" (which has exactly the momentary gap a stale,
            still-armed timer can land in).
          - EITHER case, ``OSError`` (``ftruncate``/``lseek``/``open``/
            ``dup2`` failed): :attr:`fd` is untouched, but :attr:`usable`
            becomes ``False`` — this instance will no longer be armed or
            written to (the caller must check :attr:`usable`, not
            ``fd is None``), which is what actually prevents the
            #5977-class regression a silent "keep writing, un-truncated"
            fallback would reintroduce (content growing unbounded because
            nothing ever stops the writes). Six-questions #5 ("what does
            this accumulate, who bounds it"): NOTHING accumulates — one
            fd, held for the rest of this instance's life, exactly as the
            success path already holds one; the only thing that changes
            on failure is whether writes are still attempted, not how
            many fds exist.

        #5998 history (now moot, not deleted): before this fix, this
        method DID close :attr:`fd` with no notion of whether a
        TIMER-DRIVEN writer (``faulthandler.dump_traceback_later``, via
        :class:`~reyn.runtime.loop_tripwire.StallDumpArm`) was currently
        armed against that exact fd number — a caller with such a
        consumer had to disarm it first, or a freed number could be
        handed to an unrelated file/socket/pipe moments later, with the
        pending timer then writing into THAT. #5998's own `_disarm_
        before_reset` callers are UNCHANGED (disarming before a call that
        no longer needs it is harmless, not deleted for that reason) —
        but the property "the fd number is stable" now holds
        STRUCTURALLY, independent of whether a caller remembers to
        disarm at all. This class still does not import or know about
        ``stall_trace``/``faulthandler`` — the fix does not depend on
        that either."""
        if not self.usable:
            return
        assert self._fd is not None  # `usable` already confirmed this
        if self.points_at_current_file():
            try:
                os.ftruncate(self._fd, 0)
                os.lseek(self._fd, 0, os.SEEK_SET)
            except OSError:
                self._usable = False  # fd stays open+held; just not armed again
            return
        try:
            new_fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self._MODE)
        except OSError:
            self._usable = False  # old fd stays open+held, still orphaned
            return
        try:
            os.dup2(new_fd, self._fd)
        except OSError:
            self._usable = False  # dup2 failed atomically -- self._fd untouched
        finally:
            os.close(new_fd)

    def close(self) -> None:
        """Idempotent. The one place this class intentionally releases
        the fd number — a final, explicit teardown, not the reuse-window
        hazard :meth:`reset` was fixed to avoid."""
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        self._usable = False


def diagnostic_snapshot(path: str) -> "DiagnosticSnapshot | None":
    """Open (or create) *path* as a single, always-overwritten diagnostic
    destination. ``None`` when it cannot be opened — see
    :meth:`DiagnosticSnapshot.open`."""
    return DiagnosticSnapshot.open(path)
