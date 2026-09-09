"""Tier 2: #5998 (lead-coder, real-machine hazard re-found from #5877) —
``StallDumpArm`` must disarm the process-wide ``faulthandler`` timer BEFORE
closing/reopening its snapshot's fd, at every call site that closes it, not
only :meth:`~reyn.runtime.loop_tripwire.StallDumpArm.close`.

``StallDumpArm.close`` already got this right (#5877's own original
finding): ``_disarm(); self._snapshot.close()``, documented as "the reverse
order would let a still-pending timer fire against an already-closed,
possibly already-reused fd number" — ``faulthandler.dump_traceback_later``
commits to the fd NUMBER at arm time, not a live object, so the OS handing
that number to an unrelated file/socket/pipe opened moments later turns a
stale timer into a write against THAT.

lead-coder's own audit (issue #5998) found :meth:`~reyn.runtime.
loop_tripwire.StallDumpArm.mark_fired` and the reopen-on-external-change
branch inside :meth:`~reyn.runtime.loop_tripwire.StallDumpArm.rearm` both
called ``self._snapshot.reset()`` directly — the SAME close-then-open shape
``close()`` guards, without the guard. Both are fixed here to route through
a shared ``_disarm_before_reset`` helper; this file witnesses the ORDER
(disarm strictly before the underlying ``DiagnosticSnapshot.reset()``), not
merely that disarm happens somewhere.

Real ``StallDumpArm`` + real ``DiagnosticSnapshot``/``stall_trace`` module
functions throughout — no mocks. The spy wraps the real, public
``DiagnosticSnapshot.reset`` / ``stall_trace.disarm`` and still calls
through to them (CLAUDE.md: never fake a collaborator when a real instance
is cheaply constructible), the same idiom this file's own sibling
(``test_5977_stall_dump_suppression.py``) already uses for ``StallDumpArm.
rearm`` via its local ``_counting`` helper — recording call ORDER here
instead of merely a count, since order is exactly what #5998 is about.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reyn.runtime import stall_trace
from reyn.runtime.diagnostic_snapshot import DiagnosticSnapshot
from reyn.runtime.loop_tripwire import StallDumpArm


def _open_arm(tmp_path: Path, *, label: str) -> "tuple[StallDumpArm, Path]":
    path = tmp_path / "stall_dump.log"
    arm = StallDumpArm.open(seconds=60.0, path=str(path), logger=logging.getLogger(label), label=label)
    assert arm is not None
    return arm, path


def _spy_on_disarm_and_reset(monkeypatch: pytest.MonkeyPatch) -> "list[str]":
    """Wrap the real, public ``stall_trace.disarm`` and
    ``DiagnosticSnapshot.reset`` — both still delegate to the real
    implementation; only the call ORDER is recorded."""
    order: "list[str]" = []
    real_disarm = stall_trace.disarm
    real_reset = DiagnosticSnapshot.reset

    def spy_disarm() -> None:
        order.append("disarm")
        real_disarm()

    def spy_reset(self: DiagnosticSnapshot) -> None:
        order.append("reset")
        real_reset(self)

    monkeypatch.setattr(stall_trace, "disarm", spy_disarm)
    monkeypatch.setattr(DiagnosticSnapshot, "reset", spy_reset)
    return order


def test_mark_fired_disarms_before_resetting_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5998 — an EARLIER version of ``mark_fired`` called
    ``self._snapshot.reset()`` with no disarm at all — reproducing #5877's
    own hazard (a still-armed timer's fd number freed back to the OS,
    reusable by an unrelated file/socket/pipe opened moments later).

    Strip-falsify (verified by hand: ``mark_fired`` reverted to calling
    ``self._snapshot.reset()`` directly): ``order`` ends up ``["reset"]``
    — disarm never runs at all — failing the assertion below."""
    order = _spy_on_disarm_and_reset(monkeypatch)
    arm, _path = _open_arm(tmp_path, label="t998a")
    try:
        arm.mark_fired()
    finally:
        arm.close()

    assert order[:2] == ["disarm", "reset"], (
        f"#5998 REGRESSION: mark_fired must disarm BEFORE resetting the "
        f"snapshot's fd, not after or never — got {order!r}"
    )


def test_rearm_after_external_delete_disarms_before_resetting_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5998 — the SAME hazard, through ``rearm``'s reopen-on-external-
    change branch (an external tool/operator replacing the file between
    two ticks — see ``test_5977_stall_dump_suppression.py``'s own
    ``test_stall_dump_arm_rearms_after_an_external_delete`` for the
    non-#5998 property this same branch already had a test for).

    Strip-falsify (verified by hand: the branch reverted to calling
    ``self._snapshot.reset()`` directly): ``order`` ends up ``["reset"]``
    for this branch's own reopen — failing the assertion below."""
    arm, path = _open_arm(tmp_path, label="t998b")
    try:
        assert arm.rearm() is True
        pre_ino = path.stat().st_ino

        path.unlink()
        path.write_text("", encoding="utf-8")
        assert path.stat().st_ino != pre_ino, "test setup sanity: a NEW inode"

        order = _spy_on_disarm_and_reset(monkeypatch)
        assert arm.rearm() is True

        assert order[:2] == ["disarm", "reset"], (
            f"#5998 REGRESSION: rearm's reopen-on-external-change branch "
            f"must disarm BEFORE resetting the snapshot's fd — got {order!r}"
        )
    finally:
        arm.close()
