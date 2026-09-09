"""Tier 2: #6000 ②/architect ruling — ``DiagnosticSnapshot.reset()`` never
releases its own fd NUMBER back to the OS, in either of its two cases.

Root shape (architect, #6000): ``faulthandler.dump_traceback_later`` (and
any other timer-driven writer) commits to the fd NUMBER at arm time, not a
live object — a file has an owner with a lifetime, a bare fd number does
not, so the two do not naturally agree on when it is safe to let that
number go back to the OS. #5998 closed the observed hazard by DISCIPLINE
(every call site that could close the fd disarms first) — this closes it
STRUCTURALLY instead: the number is never released at all, so there is
nothing a 4th call site could forget.

Two cases, each witnessed TWICE, for a reason found by hand while writing
this file (disclosed, not hidden): asserting the fd NUMBER
(``DiagnosticSnapshot.fd``) is unchanged before/after ``reset()`` is
architect's own explicitly-requested witness, and it IS a real, true
property of the fix — but measured by hand (this repo's real
``os.open``/``os.close`` on this machine), a quiet single-threaded test
process reliably hands the SAME fd number back to a close-then-reopen of
the SAME path anyway (the kernel's free-list returns the lowest available
number, and nothing else is allocating fds in that window) — so on its
own, the fd-number assertion does NOT distinguish the fix from the OLD
close-then-reopen shape it replaces; it would stay GREEN even reverted
(verified by hand: reverting either branch to the old close-then-reopen
shape left the number-only assertion passing). A second, DETERMINISTIC
witness closes that gap: intercepting ``os.close`` and asserting it is
NEVER called with the snapshot's own fd number during ``reset()`` — the
FIXED implementation never calls ``os.close`` on that number at all (the
common case skips it entirely; the external-change case's ``os.dup2``
closes the old number as one atomic OS-level operation, never as a
separate ``os.close()`` Python call), so this call-interception witness
is what actually falsifies a reversion to the old shape.

1. The common case — nothing external touched the path — truncated in
   place via ``os.ftruncate``/``os.lseek``, no close at all.
2. The external-change case — something deleted/replaced the path since
   this fd was opened — reopened via ``os.dup2`` onto the SAME number
   (POSIX-atomic: architect's own disclosed limitation is that ``dup2``'s
   atomicity is a POSIX guarantee, not something reyn has measured
   itself in this repo).

Real files, real fds throughout (no mocks of ``DiagnosticSnapshot`` or
domain collaborators) — the same no-mocks posture
``test_5998_disarm_before_reset.py`` already established for this module.
``os.close`` itself is intercepted (not faked — the real close still
runs; only the CALL is recorded) purely to observe which fd numbers it
is invoked with, the same "wrap the real thing, only record" shape that
file's own ``_spy_on_disarm_and_reset`` already uses for
``DiagnosticSnapshot.reset``/``stall_trace.disarm``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot


def test_reset_on_an_untouched_file_never_changes_the_fd_number(tmp_path: Path) -> None:
    """Tier 2: the common case — `reset()` truncates in place; the fd
    NUMBER `DiagnosticSnapshot.fd` exposes is bit-for-bit identical
    before and after, across MULTIPLE resets in a row.

    NOT independently falsifiable (disclosed, module docstring): a quiet
    single-threaded test process hands the SAME number back even from
    the OLD close-then-reopen shape (verified by hand), so this test
    alone would stay green reverted. Kept anyway as a real, true
    property of the fix — paired with
    `test_the_common_case_never_calls_os_close_on_its_own_fd`, which IS
    the falsifiable witness for this same case."""
    snapshot = DiagnosticSnapshot.open(str(tmp_path / "snap.log"))
    assert snapshot is not None
    before = snapshot.fd

    for _ in range(3):
        snapshot.reset()
        assert snapshot.fd == before, (
            f"#6000 REGRESSION: reset() on an untouched file changed the fd "
            f"number ({before} -> {snapshot.fd}) — the number was released "
            f"back to the OS at some point during reset()"
        )


def test_reset_actually_truncates_the_file_content(tmp_path: Path) -> None:
    """Tier 2: falsification contrast for the test above — the in-place
    `ftruncate`+`lseek` path must still perform a REAL truncate, not just
    happen to keep the same fd number while leaving stale content behind
    (the property `reset()` exists for in the first place)."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    assert snapshot.fd is not None
    os.write(snapshot.fd, b"stale dump content")
    assert path.read_bytes() == b"stale dump content"

    snapshot.reset()

    assert path.read_bytes() == b"", (
        "#6000 REGRESSION: reset() must still truncate the file's real "
        "content, not merely preserve the fd number"
    )


def test_reset_after_external_delete_keeps_the_same_fd_number(tmp_path: Path) -> None:
    """Tier 2: the external-change case — the path was deleted and
    recreated (a NEW inode) by something outside this instance's control
    since the fd was opened; `reset()` must still hand back the SAME fd
    NUMBER (`os.dup2` onto it), never a freshly `os.open`-ed one.

    NOT independently falsifiable (disclosed, module docstring): the
    same "quiet process reuses the lowest freed number anyway" effect
    applies here too (verified by hand). Kept as a real, true property
    of the fix — paired with
    `test_the_external_change_case_never_calls_os_close_on_its_own_fd`,
    which IS the falsifiable witness for this same case."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    before = snapshot.fd
    pre_ino = path.stat().st_ino

    path.unlink()
    path.write_text("", encoding="utf-8")
    assert path.stat().st_ino != pre_ino, "test setup sanity: a NEW inode at the same path"
    assert not snapshot.points_at_current_file(), (
        "test setup sanity: the snapshot's own fd must now point at the "
        "UNLINKED (deleted) inode, not the new one at the same path"
    )

    snapshot.reset()

    assert snapshot.fd == before, (
        f"#6000 REGRESSION: reset() after an external delete/recreate "
        f"changed the fd number ({before} -> {snapshot.fd}) — dup2 onto "
        f"the old number did not happen, or the old number was released "
        f"first"
    )
    assert snapshot.points_at_current_file(), (
        "reset() must leave the fd pointing at the file CURRENTLY at "
        "path, not the deleted one it was originally opened against"
    )


def test_reset_after_external_delete_writes_land_in_the_new_file(tmp_path: Path) -> None:
    """Tier 2: functional-correctness contrast for the test above — same
    fd NUMBER is necessary but not sufficient; a write after reset() must
    actually reach the NEW file on disk, not silently vanish into the
    orphaned (deleted) one dup2 replaced."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    assert snapshot.fd is not None

    path.unlink()
    path.write_text("", encoding="utf-8")
    snapshot.reset()

    assert snapshot.fd is not None
    os.write(snapshot.fd, b"a fresh dump")

    assert path.read_bytes() == b"a fresh dump", (
        "#6000 REGRESSION: a write after reset()'s dup2-based reopen did "
        "not land in the file currently at path"
    )


def _spy_on_os_close(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    """Wrap the real, public ``os.close`` — the real close still runs;
    only the fd NUMBER it was called with is recorded. Same "wrap the
    real thing, only record" shape as
    ``test_5998_disarm_before_reset.py``'s own ``_spy_on_disarm_and_reset``."""
    calls: "list[int]" = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)
    return calls


def test_the_common_case_never_calls_os_close_on_its_own_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the DETERMINISTIC witness the fd-number tests above cannot
    be on their own (module docstring) — the fd-number assertion stays
    green even reverted to the old close-then-reopen shape (a quiet test
    process hands the same freed number straight back); this does not.
    The fixed implementation never calls `os.close` with the snapshot's
    own fd number in the common case at all — `os.ftruncate`/`os.lseek`
    truncate in place.

    Strip-falsifier (verified by hand: the common-case branch reverted
    to `os.close(self._fd); self._fd = os.open(...)`): this test goes
    red — `own_fd in calls` becomes `True`."""
    snapshot = DiagnosticSnapshot.open(str(tmp_path / "snap.log"))
    assert snapshot is not None
    own_fd = snapshot.fd
    calls = _spy_on_os_close(monkeypatch)

    snapshot.reset()

    assert own_fd not in calls, (
        f"#6000 REGRESSION: reset() on an untouched file called os.close("
        f"{own_fd}) — the fd number was momentarily released back to the "
        f"OS, exactly the window dup2's atomicity exists to close"
    )


def test_the_external_change_case_never_calls_os_close_on_its_own_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the same deterministic witness for the external-change
    branch — `os.dup2(new_fd, old_fd)` closes `old_fd` as ONE atomic
    OS-level operation, never as a separate Python-level `os.close()`
    call, so this stays clean for the fixed implementation and catches a
    reversion to close-then-reopen the same way the test above does.

    Strip-falsifier (verified by hand: the `os.dup2` branch reverted to
    `os.close(self._fd); self._fd = os.open(...)`): this test goes red —
    `own_fd in calls` becomes `True`."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    own_fd = snapshot.fd

    path.unlink()
    path.write_text("", encoding="utf-8")
    assert not snapshot.points_at_current_file(), (
        "test setup sanity: the snapshot's own fd must point at the "
        "unlinked (deleted) inode before reset()"
    )

    calls = _spy_on_os_close(monkeypatch)
    snapshot.reset()

    assert own_fd not in calls, (
        f"#6000 REGRESSION: reset() after an external delete/recreate "
        f"called os.close({own_fd}) — the fd number was momentarily "
        f"released back to the OS instead of dup2'd onto in place"
    )
