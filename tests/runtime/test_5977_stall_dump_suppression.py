"""Tier 2: #5977 ①②③ — the stall-dump self-amplification fix.

``watch_event_loop`` re-armed ``StallDumpArm`` unconditionally every tick,
with zero coupling to ``LoopTripwire``'s own episode-level state — a
long-perceived-as-one stall episode is actually many separate tick-level
arm→(maybe fire)→rearm cycles, each of which can independently dump the
full thread stack, and each dump's own synchronous write cost adds to the
very lateness that risks triggering the next one (owner-hit, the log
"ひたすら繰り返されてる"). ① caps this at one dump per stall episode.

② moved the dump out of ``reyn.log`` entirely into its own single,
always-overwritten :func:`~reyn.runtime.diagnostic_snapshot.diagnostic_
snapshot` — real-machine measurement found dump lines were 49% of the
owner's own ``reyn.log``. There is deliberately NO session-total cap
(dropped from an earlier iteration of this PR, architect self-correction):
a cap would have frozen the dump file at whichever stall happened to be
the Nth, silently hiding every LATER, possibly more diagnostically
valuable one — the ONE THING ②'s always-overwritten shape exists to
guarantee never happens.

The clock is an INPUT here too (CLAUDE.md: no duration in a test, in either
direction) — reuses ``test_5898_loop_tripwire.py``'s own ``_ScriptedClock``
shape rather than a second copy.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot, diagnostic_snapshot
from reyn.runtime.loop_tripwire import LoopTripwire, StallDumpArm, stall_dump_path, watch_event_loop


class _ScriptedClock:
    """See ``test_5898_loop_tripwire.py``'s own copy for the full
    rationale — duplicated rather than imported across test files per this
    repo's own test-isolation convention (no shared test-only helper
    module for a two-file, two-line class)."""

    def __init__(self, instants: "list[float]") -> None:
        self._instants = list(instants)

    def __call__(self) -> float:
        if not self._instants:
            raise asyncio.CancelledError
        return self._instants.pop(0)

    async def sleep(self, _seconds: float) -> None:
        if not self._instants:
            raise asyncio.CancelledError


def _open_arm(tmp_path: Path, *, label: str) -> "tuple[StallDumpArm, Path]":
    path = tmp_path / "stall_dump.log"
    arm = StallDumpArm.open(seconds=0.25, path=str(path), logger=logging.getLogger(label), label=label)
    assert arm is not None
    return arm, path


def _counting(method):
    """Wrap a bound method with a call counter that still delegates to the
    real implementation — a real collaborator, spied on, never mocked
    (CLAUDE.md: never fake a collaborator when a real instance is cheaply
    constructible)."""
    calls = [0]

    def wrapper(*args, **kwargs):
        calls[0] += 1
        return method(*args, **kwargs)

    return wrapper, calls


def _write_a_real_marker_on_every_recorded_dump(tripwire: LoopTripwire, arm: StallDumpArm) -> "list[bytes]":
    """Wire ``tripwire.record_stack_dump`` to write a REAL, distinguishable
    marker into ``arm``'s own fd at the exact point production code
    detects a fire — standing in for the real (timer-driven, unwaitable
    in a scripted-clock test) ``faulthandler`` write that would land
    there.

    #5992 (lead-coder review of PR #5988): counting HOW MANY TIMES
    ``mark_fired()`` was called (this file's earlier form) verifies the
    call happened, never that it had any EFFECT — a build that truncates
    on the WRONG tick (or never at all) can still make every one of those
    call-count assertions pass. Checking the file's own FINAL content
    against ``markers[-1]`` (not merely "the last marker is present" —
    every EARLIER one must be GONE too) is the real witness."""
    markers: "list[bytes]" = []
    real_record = tripwire.record_stack_dump

    def record_and_write() -> None:
        assert arm.fd is not None, "the arm must still be armed when a dump is recorded"
        marker = f"dump #{len(markers) + 1}\n".encode()
        markers.append(marker)
        os.write(arm.fd, marker)
        real_record()

    tripwire.record_stack_dump = record_and_write  # type: ignore[method-assign]
    return markers


def _many_episode_clock_instants(n: int) -> "list[float]":
    """*n* separate stall episodes, each a late tick (+350 ms) followed by
    a recovering one (+50 ms, on time) — a fresh dump allowance per
    episode, ending in recovery so ``n`` is unambiguous (no still-ongoing
    episode at the end to miscount)."""
    instants = [0.0, 0.05]
    t = 0.05
    for _ in range(n):
        t += 0.40
        instants.append(t)
        t += 0.05
        instants.append(t)
    return instants


@pytest.mark.asyncio
async def test_watch_event_loop_dumps_at_most_once_per_stall_episode(tmp_path: Path) -> None:
    """Tier 2: #5977 ①. Clock script: healthy(0.05) → late +350ms(0.45, onset) → late
    +350ms(0.85, the SAME still-ongoing episode — no recovery in between)
    → recovered(0.90). TWO over-threshold ticks, one episode.

    Without ①'s gate, ``StallDumpArm.rearm()`` runs every tick
    unconditionally, so BOTH over-threshold ticks would read back
    ``stack_dumped=True`` — the issue's own reported pattern (many
    consecutive ``Thread 0x`` blocks for one stall). With it, the SECOND
    over-threshold tick's re-arm is skipped entirely
    (``should_arm_stack_dump()`` is ``False`` once the episode already
    dumped — there is no separate ``faulthandler`` state to cancel;
    skipping the re-arm IS the suppression), so ``mark_fired`` (the ②
    truncate-for-next-episode call, 1:1 with an observed dump) fires
    exactly once for the whole episode.

    Strip-falsify (verified by hand: the ``tripwire.should_arm_stack_dump()``
    read in the post-``observe()`` re-arm decision temporarily forced to
    ``True``): a SECOND marker gets written for this exact script — the
    issue's own accept criterion ("抑制を外すと、同じ stall で複数セット
    出ることを確認").

    ⚠️ This assertion alone does NOT witness the SEPARATE ordering defect
    lead-coder found in review (PR #5980 BLOCKING): whether re-arming is
    decided BEFORE vs. AFTER this tick's own detection can leave a REAL,
    uncancelled ``faulthandler`` timer pending even while the dump COUNT
    stays byte-identical — a script that RECOVERS masks the difference
    (the recovery tick's own legitimate re-arm makes the call count
    converge either way). See
    ``test_watch_event_loop_never_arms_a_second_timer_before_recording_the_first_dump``
    below, which ends WHILE STILL IN THE STALL (no recovery tick), for
    the script that actually separates the two orderings."""
    arm, path = _open_arm(tmp_path, label="t1")
    rearm_wrapper, rearm_calls = _counting(arm.rearm)
    arm.rearm = rearm_wrapper  # type: ignore[method-assign]

    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.85, 0.90])
    tripwire = LoopTripwire(threshold_ms=250.0)
    markers = _write_a_real_marker_on_every_recorded_dump(tripwire, arm)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
        final_content = path.read_bytes()
    finally:
        arm.close()

    assert markers == [b"dump #1\n"], "one over-threshold tick already dumped; the second must not"
    assert final_content == markers[-1], (
        "the file must hold ONLY the last dump's marker (no earlier content lingering)"
    )
    assert rearm_calls[0] == 3, (
        "initial arm + the ONE tick that dumped — the second over-threshold "
        "tick's re-arm must be skipped, not just its readback ignored"
    )


@pytest.mark.asyncio
async def test_a_second_stall_episode_gets_its_own_fresh_dump_allowance(tmp_path: Path) -> None:
    """Tier 2: #5977 ①. Recovery must reset the per-episode gate: a
    SECOND, separate stall episode after a full recovery gets its own
    dump. Script: healthy → late(onset A, dump#1) → recovered(A) →
    late(onset B, dump#2) → recovered(B). #5992: the file's own FINAL
    content must hold ONLY B's marker — A's must be gone, not sitting
    beside it."""
    arm, path = _open_arm(tmp_path, label="t2")
    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.50, 0.90, 0.95])
    tripwire = LoopTripwire(threshold_ms=250.0)
    markers = _write_a_real_marker_on_every_recorded_dump(tripwire, arm)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
        final_content = path.read_bytes()
    finally:
        arm.close()

    assert markers == [b"dump #1\n", b"dump #2\n"], "two SEPARATE episodes each get their own one-shot dump"
    assert final_content == markers[-1], "episode B's dump must have overwritten episode A's, not sat beside it"


@pytest.mark.asyncio
async def test_no_session_cap_the_12th_episode_still_dumps(tmp_path: Path) -> None:
    """Tier 2: #5977 ② (architect self-correction). An EARLIER iteration
    of this PR carried a session-total cap of 10 (①'s own original
    reason: dumps were pushing operational ``reyn.log`` lines out of a
    bounded rotation window). ② removes that reason entirely — the dump
    now lives in its OWN always-overwritten file, so a later stall's dump
    is never a worse sample than an earlier one it replaces, and a
    session cap would instead have frozen the file at the Nth stall,
    hiding every later one. This test runs 12 SEPARATE episodes (past the
    old cap of 10) and asserts all 12 dump.

    Strip-falsify #1 (verified by hand: re-adding ``if self._session_dump_
    count >= 10: return False`` to ``should_arm_stack_dump``): only 10
    markers get written for this exact script instead of 12 — the
    issue's own accept criterion ("11 回目以降も dump ファイルが最新に
    更新される").

    ⚠️ #5992 (lead-coder review of PR #5988): counting HOW MANY TIMES
    ``mark_fired()`` was called (this test's earlier form) verifies the
    call happened, never that it had any EFFECT — a build that truncates
    the WRONG episode's dump (or the RIGHT one on the WRONG tick) can
    still leave every one of those call-count assertions green. This
    test instead checks the file's own FINAL content against the LAST
    episode's marker alone. Strip-falsify #2 (verified by hand:
    ``mark_fired()`` calls moved back to firing immediately upon
    detecting EACH episode's own dump, rather than deferred to the START
    of the NEXT episode): the file ends up EMPTY (episode 12's own
    content gets destroyed by its OWN immediate truncation) instead of
    holding episode 12's marker — failing the equality assertion below."""
    arm, path = _open_arm(tmp_path, label="t3")
    clock = _ScriptedClock(_many_episode_clock_instants(12))
    tripwire = LoopTripwire(threshold_ms=250.0)
    markers = _write_a_real_marker_on_every_recorded_dump(tripwire, arm)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
        final_content = path.read_bytes()
    finally:
        arm.close()

    assert markers == [f"dump #{i}\n".encode() for i in range(1, 13)], (
        "no cap — every one of 12 separate episodes must dump"
    )
    assert final_content == markers[-1], (
        "the file must hold ONLY episode 12's marker — every earlier one must have "
        "been truncated away by mark_fired(), not accumulated beside it, and episode "
        "12's own marker must have survived (not destroyed by its own truncation)"
    )


@pytest.mark.asyncio
async def test_watch_event_loop_never_arms_a_second_timer_before_recording_the_first_dump(
    tmp_path: Path,
) -> None:
    """Tier 2: #5977's actual reported bug (lead-coder BLOCKING, PR #5980)
    — a script that RECOVERS masks it (the recovery tick's own legitimate
    re-arm converges the call count either way, see the earlier test's
    own note). This script ends WHILE STILL IN THE STALL instead: healthy
    → late(0.45, onset, dump#1) → late(0.85, still stall) → late(1.25,
    still stall) — three consecutive over-threshold ticks, no recovery.

    Correct (decide re-arm AFTER ``observe()``): the tick that detects
    dump#1 (0.45) has ALREADY closed the episode's allowance by the time
    it decides whether to re-arm, so it does not — 2 total ``rearm()``
    calls (init + the one healthy tick). Buggy (decide BEFORE
    ``observe()``, checking "should I arm" and "did the arm I just made
    fire" together): the SAME tick both re-arms unconditionally AND
    detects the fire — a 3rd, uncancelled call, a real live timer left
    pending for no reason. Strip-falsify (verified by hand, both
    directions): swapping the re-arm decision back to before ``observe()``
    in ``watch_event_loop`` turns this 2 into 3."""
    arm, path = _open_arm(tmp_path, label="t4")
    rearm_wrapper, rearm_calls = _counting(arm.rearm)
    arm.rearm = rearm_wrapper  # type: ignore[method-assign]

    clock = _ScriptedClock([0.0, 0.05, 0.45, 0.85, 1.25])
    tripwire = LoopTripwire(threshold_ms=250.0)
    markers = _write_a_real_marker_on_every_recorded_dump(tripwire, arm)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_event_loop(
                tripwire,
                on_stall=lambda _ms: None,
                stack_dump=arm,
                tick_seconds=0.05,
                clock=clock,
                sleep=clock.sleep,
            )
        final_content = path.read_bytes()
    finally:
        arm.close()

    assert markers == [b"dump #1\n"]
    assert final_content == markers[-1]
    assert rearm_calls[0] == 2, (
        "the tick that detects the first dump must not ALSO arm a second, "
        "uncancelled timer for the still-ongoing stall"
    )


def test_diagnostic_snapshot_reset_truncates_for_the_next_write(tmp_path: Path) -> None:
    """Tier 2: #5978 ①'s general shape — a real, real-fd-backed
    ``DiagnosticSnapshot``. Writing then :meth:`~DiagnosticSnapshot.reset`
    leaves the file empty and ready for a NEXT write at the same fd
    number — the exact contract :class:`StallDumpArm` depends on to
    overwrite in place. Strip-falsify: dropping the ``O_TRUNC`` flag from
    ``reset``'s reopen would leave the OLD content (plus whatever the
    next write appends after it) instead of an empty file — verified by
    hand."""
    path = tmp_path / "snap.log"
    snap = diagnostic_snapshot(str(path))
    assert snap is not None
    try:
        os.write(snap.fd, b"first dump\n")
        assert path.read_bytes() == b"first dump\n"

        snap.reset()
        assert path.read_bytes() == b"", "reset must truncate — the OLD dump must not linger"

        assert snap.fd is not None
        os.write(snap.fd, b"second dump\n")
        assert path.read_bytes() == b"second dump\n", "the reopened fd must be writable for the NEXT dump"
    finally:
        snap.close()


def test_diagnostic_snapshot_points_at_current_file_detects_an_external_delete(tmp_path: Path) -> None:
    """Tier 2: #5977 ②, lead-coder BLOCKING follow-up (PR #5988 review) —
    dropping the rotation MECHANISM (② moved the dump off ``reyn.log``,
    the only thing ever rotating it) does not remove the CLASS: an
    external tool (a cleanup script, an operator ``rm``) can still delete
    or replace ``stall_dump.log`` out from under an already-open fd. A
    write against that orphaned fd still SUCCEEDS — silently, into a file
    nobody can find via the known path — so :meth:`~DiagnosticSnapshot.
    points_at_current_file` must catch this the same way a rotation check
    would, generalized to any cause. Strip-falsify: comparing ``self.
    _fd`` against ``self._path`` by name instead of inode would stay
    ``True`` here (the PATH is unchanged — a new file was simply created
    there), which is exactly the bug this method exists to avoid."""
    path = tmp_path / "stall_dump.log"
    snap = diagnostic_snapshot(str(path))
    assert snap is not None
    try:
        assert snap.points_at_current_file() is True

        path.unlink()
        path.write_text("", encoding="utf-8")  # a NEW file, same path, different inode
        assert snap.points_at_current_file() is False, (
            "an external delete+recreate at the SAME path must be detected"
        )

        snap.reset()
        assert snap.points_at_current_file() is True, "reset must reopen against the CURRENT file"
    finally:
        snap.close()


@pytest.mark.asyncio
async def test_stall_dump_arm_rearms_after_an_external_delete(tmp_path: Path) -> None:
    """Tier 2: the same property as the ``DiagnosticSnapshot`` test above,
    through :class:`StallDumpArm.rearm`'s own real call — the actual
    production path this reasoning has to hold on. Deleting the dump file
    between two ``rearm()`` calls must not leave the arm silently writing
    into an orphaned fd: the second ``rearm()`` reopens first."""
    path = tmp_path / "stall_dump.log"
    arm = StallDumpArm.open(seconds=60.0, path=str(path), logger=logging.getLogger("t5"), label="t5")
    assert arm is not None
    try:
        assert arm.rearm() is True
        pre_ino = path.stat().st_ino

        path.unlink()
        path.write_text("", encoding="utf-8")
        assert path.stat().st_ino != pre_ino, "test setup sanity: the replacement must be a NEW inode"

        assert arm.rearm() is True
        assert arm.points_at_current_file() is True, (
            "rearm must reopen against the file CURRENTLY at path, not keep "
            "writing into the orphaned pre-delete fd"
        )
    finally:
        arm.close()


def test_diagnostic_snapshot_open_returns_none_on_an_unwritable_path() -> None:
    """Tier 2: a path under a directory that cannot be created (a FILE
    sitting where a directory needs to go) fails closed — ``None``, no
    exception — matching :class:`StallDumpArm`'s own "no genuinely stable
    destination, no attempt" posture for a missing log path."""
    with __import__("tempfile").TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "not_a_directory"
        blocker.write_text("", encoding="utf-8")
        assert diagnostic_snapshot(str(blocker / "sub" / "snap.log")) is None


def test_diagnostic_snapshot_has_no_rotation_or_config_surface() -> None:
    """Tier 1: structural — #5978's own accept criterion ("snapshot の
    書き口に rotation / 世代 / 設定項目が 1 つも無い"). ``DiagnosticSnapshot``
    takes exactly one constructor argument beyond its own fd/path; no
    ``backup_count``, ``max_bytes``, or similar. A future PR that adds one
    would need to touch this assertion, which is the point."""
    import inspect

    params = set(inspect.signature(DiagnosticSnapshot.open).parameters)
    assert params == {"path"}, "no config surface beyond the destination itself"


def test_stall_dump_path_is_scoped_to_this_process_not_the_workspace() -> None:
    """Tier 1: #5992 (lead-coder review of PR #5988) — a fixed, PID-less
    filename here would make every process sharing a ``reyn.log``
    directory (measured live: ``reyn:web`` and ``reyn:chat`` attached to
    the SAME project) point their own ``DiagnosticSnapshot`` at the SAME
    inode; since ``DiagnosticSnapshot`` opens ``O_TRUNC`` (not
    ``O_APPEND``), two processes would each write from independent
    offset 0 — one silently overwriting the other, or the two writes
    interleaving into a torn, unreadable dump. Embedding ``os.getpid()``
    makes each process's own destination structurally distinct — not a
    config surface (nobody SETS a pid; it is read, never chosen).

    Non-vacuity / strip-falsify: a build that reverted to the fixed
    ``"stall_dump.log"`` name would make this equality assertion pass
    with an UNCHANGED path for every pid — this test's own second
    assertion (the pid's digits appear in the result) is what a fixed
    name fails, verified by hand (temporarily reverting the f-string to
    the literal filename)."""
    result = stall_dump_path("/tmp/example/.reyn/logs/reyn.log")
    assert result is not None
    assert result == f"/tmp/example/.reyn/logs/stall_dump.{os.getpid()}.log"
    assert str(os.getpid()) in result, "the pid must actually appear in the derived filename"
