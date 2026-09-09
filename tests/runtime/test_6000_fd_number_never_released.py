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

import logging
import os
from pathlib import Path

import pytest

from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot
from reyn.runtime.loop_tripwire import StallDumpArm


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


# ---------------------------------------------------------------------------
# lead-coder review of this PR's first version: the two SUCCESS paths above
# are not "either of its two cases" -- an OSError inside either one is a
# real THIRD case, and the first version's own except branches closed the
# fd (common case) or silently leaked it (external-change case) on
# failure, both routes this method's own docstring claimed did not exist.
# ---------------------------------------------------------------------------


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def test_a_failed_truncate_never_closes_or_nulls_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the common-case failure path — an `os.ftruncate` OSError
    must leave `fd` untouched: same number, still genuinely OPEN (not a
    stale int left over from a closed fd), never `None`.

    Strip-falsifier (verified by hand: the `except OSError` branch
    reverted to `os.close(self._fd); self._fd = None`): this test goes
    red — `snapshot.fd` becomes `None`."""
    snapshot = DiagnosticSnapshot.open(str(tmp_path / "snap.log"))
    assert snapshot is not None
    before = snapshot.fd

    def failing_ftruncate(fd: int, length: int) -> None:
        raise OSError("simulated ftruncate failure")

    monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
    snapshot.reset()

    assert snapshot.fd == before, (
        f"#6000 REGRESSION: a failed truncate changed fd ({before} -> "
        f"{snapshot.fd}) instead of leaving it untouched"
    )
    assert snapshot.fd is not None and _is_open(snapshot.fd), (
        "#6000 REGRESSION: a failed truncate left fd as a closed/stale "
        "number, not a genuinely open one"
    )


def test_a_failed_external_reopen_never_closes_or_nulls_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the external-change failure path — an `os.open` OSError
    (reopening the NEW file at *path*) must leave the OLD fd untouched:
    same number, still open, never `None`, never silently dropped.

    Strip-falsifier (verified by hand: the `except OSError` branch
    reverted to `self._fd = None` with no `os.close` at all — a genuine
    LEAK, not merely a stale value): this test goes red — `snapshot.fd`
    becomes `None` (the leak itself is invisible to THIS assertion, which
    is exactly why the number/None check matters here, not fstat)."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    before = snapshot.fd

    path.unlink()
    path.write_text("", encoding="utf-8")

    def failing_open(*args: object, **kwargs: object) -> int:
        raise OSError("simulated reopen failure")

    monkeypatch.setattr(os, "open", failing_open)
    snapshot.reset()

    assert snapshot.fd == before, (
        f"#6000 REGRESSION: a failed external reopen changed fd "
        f"({before} -> {snapshot.fd}) instead of leaving the OLD one "
        f"untouched"
    )
    assert snapshot.fd is not None and _is_open(snapshot.fd), (
        "#6000 REGRESSION: a failed external reopen left fd as a closed/"
        "stale number, not a genuinely open one"
    )


def test_a_failed_dup2_never_closes_or_nulls_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the external-change branch's own `dup2` failure — POSIX
    guarantees `dup2` either succeeds atomically or leaves `oldfd`
    unmodified on failure; this must not additionally close or null it
    out on the Python side.

    Strip-falsifier (verified by hand: the `dup2` call left un-caught,
    letting the raised `OSError` propagate out of `reset()`): this test
    goes red — the call raises instead of returning."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None
    before = snapshot.fd

    path.unlink()
    path.write_text("", encoding="utf-8")

    real_dup2 = os.dup2

    def failing_dup2(fd: int, fd2: int) -> int:
        # Scoped to THIS snapshot's own fd only -- pytest's own fd-capture
        # teardown machinery also calls the real os.dup2, and a blanket
        # patch would break it (observed by hand: an unscoped patch here
        # crashes pytest's own "Captured stdout teardown" step).
        if fd2 == before:
            raise OSError("simulated dup2 failure")
        return real_dup2(fd, fd2)

    monkeypatch.setattr(os, "dup2", failing_dup2)
    snapshot.reset()

    assert snapshot.fd == before, (
        f"#6000 REGRESSION: a failed dup2 changed fd ({before} -> "
        f"{snapshot.fd}) instead of leaving it untouched"
    )
    assert snapshot.fd is not None and _is_open(snapshot.fd), (
        "#6000 REGRESSION: a failed dup2 left fd as a closed/stale "
        "number, not a genuinely open one"
    )


def test_open_creates_the_file_with_mode_0o600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect review — `0o600`, deliberately NARROWER than
    the historical `open(path, "a")`'s own default (`0o666`), not merely
    matched to it: a stall/diagnostic dump can contain a path or argv,
    which is worth keeping unreadable by other local users. (`os.open`'s
    own default, `0o777` masked by umask, would be wider still.)"""
    real_open = os.open
    modes: "list[int]" = []

    def spy_open(path: str, flags: int, mode: int = 0o777, *a: object, **kw: object) -> int:
        modes.append(mode)
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    snapshot = DiagnosticSnapshot.open(str(tmp_path / "snap.log"))
    assert snapshot is not None

    assert modes == [0o600], (
        f"#6000 REGRESSION: DiagnosticSnapshot.open() did not pass an "
        f"explicit mode=0o600 to os.open — got {modes!r}"
    )


def test_external_reopen_creates_the_file_with_mode_0o600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the same mode fix, for `reset()`'s own external-change
    reopen call site — a second `os.open` call this class makes, easy to
    miss fixing only the first one."""
    path = tmp_path / "snap.log"
    snapshot = DiagnosticSnapshot.open(str(path))
    assert snapshot is not None

    path.unlink()
    path.write_text("", encoding="utf-8")

    real_open = os.open
    modes: "list[int]" = []

    def spy_open(p: str, flags: int, mode: int = 0o777, *a: object, **kw: object) -> int:
        modes.append(mode)
        return real_open(p, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    snapshot.reset()

    assert modes == [0o600], (
        f"#6000 REGRESSION: reset()'s external-change reopen did not "
        f"pass an explicit mode=0o600 to os.open — got {modes!r}"
    )


# ---------------------------------------------------------------------------
# lead-coder + architect ruling: `_fd = None` was conflating "don't have a
# number" with "not safe to use" -- once reset() stopped ever releasing the
# number, those two facts diverged. `usable` is the new, separate answer to
# "may this snapshot be armed/written to." Witnessed here at the
# StallDumpArm level (the real caller), not just DiagnosticSnapshot's own.
# ---------------------------------------------------------------------------


def test_snapshot_becomes_unusable_but_keeps_its_fd_after_a_failed_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the two facts, witnessed together on the SAME failure — a
    failed reset leaves `fd` unchanged (the number is held, not
    released) AND `usable` false (do not write here again)."""
    snapshot = DiagnosticSnapshot.open(str(tmp_path / "snap.log"))
    assert snapshot is not None
    before = snapshot.fd
    assert snapshot.usable is True

    def failing_ftruncate(fd: int, length: int) -> None:
        raise OSError("simulated ftruncate failure")

    monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
    snapshot.reset()

    assert snapshot.fd == before, (
        "#6000 REGRESSION: a failed reset must not change the fd number"
    )
    assert snapshot.usable is False, (
        "#6000 REGRESSION: a failed reset must mark the snapshot unusable "
        "-- fd staying open is not the same as safe to write to again"
    )


def test_stall_dump_arm_refuses_to_rearm_after_a_failed_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the real caller — `StallDumpArm.rearm()` must decline (not
    silently arm against a stale, un-truncated fd) once its own snapshot
    has gone unusable, and `armed` must reflect that too. This is the
    property that actually prevents the #5977-class regression a silent
    "keep writing, un-truncated" fallback would have reintroduced: no
    arm means no more writes at all, not merely un-truncated ones.

    `armed` witnessed by RETURN VALUE (strip-falsifier, verified by hand:
    `armed`'s own check reverted to `fd is None`): goes red directly --
    `armed` reads `True` even though the snapshot is unusable.

    `rearm`'s own FIRST guard (`if not usable: return False`) is
    NOT witnessed by its return value -- lead-coder review of this PR's
    first version: with that guard alone removed, `points_at_current_
    file()` (unaffected -- it checks `usable` internally too) still
    returns `False`, so `rearm` still enters its reopen branch, still
    hits its OWN second `usable` check there, and still returns `False`
    in the end -- the SAME observable return value either way. What
    actually differs is called EVERY TICK on an already-unusable
    snapshot without the first guard: `_disarm_before_reset()` runs (a
    real disarm of the process-wide timer) and an ERROR is logged, both
    unboundedly, on every single `rearm()` call from then on -- the
    exact "reyn.log fills with a repeated line" class #5977 closed.
    Witnessed here on THAT axis instead: disarm-call count and log
    record count across MULTIPLE `rearm()` calls, not the return value."""
    path = tmp_path / "stall_dump.log"
    arm = StallDumpArm.open(seconds=60.0, path=str(path), logger=logging.getLogger("t6000"), label="t6000")
    assert arm is not None
    try:
        assert arm.rearm() is True
        assert arm.armed is True

        def failing_ftruncate(fd: int, length: int) -> None:
            raise OSError("simulated ftruncate failure")

        monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
        # mark_fired() is the real call site that reaches reset() on the
        # common (untouched-file) path -- same shape test_5998's own
        # test_mark_fired_disarms_before_resetting_the_snapshot uses.
        arm.mark_fired()

        assert arm.armed is False, (
            "#6000 REGRESSION: armed must go False once the snapshot's "
            "own reset failed -- fd staying open (never released) must "
            "not read as still armed"
        )

        from reyn.runtime import stall_trace

        disarm_calls: "list[None]" = []
        real_disarm = stall_trace.disarm

        def spy_disarm() -> None:
            disarm_calls.append(None)
            real_disarm()

        monkeypatch.setattr(stall_trace, "disarm", spy_disarm)
        caplog.set_level(logging.ERROR, logger="t6000")

        for _ in range(3):
            assert arm.rearm() is False, (
                "#6000 REGRESSION: rearm() must refuse to arm against an "
                "unusable snapshot -- writing un-truncated content is the "
                "#5977 regression this refusal exists to prevent"
            )

        assert disarm_calls == [], (
            f"#6000 REGRESSION: rearm()'s first `usable` guard must short-"
            f"circuit BEFORE any disarm -- an already-unusable snapshot "
            f"must not re-disarm the process-wide timer on every tick "
            f"(the #5977-class log/disarm-flood this guard exists to "
            f"prevent) — got {len(disarm_calls)} disarm call(s) across 3 "
            f"rearm() calls"
        )
        assert caplog.records == [], (
            f"#6000 REGRESSION: rearm() on an already-unusable snapshot "
            f"logged {len(caplog.records)} record(s) across 3 calls -- "
            f"the same repeated-ERROR-line shape #5977 closed, reintroduced "
            f"here if the first `usable` guard is skipped"
        )
    finally:
        arm.close()
